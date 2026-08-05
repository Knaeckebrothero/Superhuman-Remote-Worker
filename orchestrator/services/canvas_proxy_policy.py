"""Pure security policy for the Dynamic Canvas ordinary-HTTP gateway.

This module deliberately has no FastAPI, database, or workspace-transport
dependency.  A future dedicated Canvas gateway can validate the browser-facing
request here, authenticate its viewer session, spool the complete request body,
and only then pass the resulting :class:`CanvasUpstreamRequest` to the h11
transport adapter.

The first live-preview delivery is cookie-free for application traffic.  The
reserved, host-only viewer cookie is extracted for the gateway but is never
forwarded to the workspace application.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Sequence
from urllib.parse import quote, quote_plus, unquote_to_bytes, urlsplit

from services.canvas_apps import CanvasAppError, canonical_canvas_app_path

_TOKEN = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_MALFORMED_PERCENT = re.compile(rb"%(?![0-9A-Fa-f]{2})")
_REMAINING_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")

ALLOWED_CANVAS_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)

_HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"expect",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
_PLATFORM_PRIVATE = frozenset(
    {
        b"authorization",
        b"baggage",
        b"cf-connecting-ip",
        b"client-ip",
        b"fastly-client-ip",
        b"fly-client-ip",
        b"forwarded",
        b"grpc-trace-bin",
        b"referer",
        b"traceparent",
        b"tracestate",
        b"true-client-ip",
        b"uber-trace-id",
        b"via",
        b"x-access-token",
        b"x-csrf",
        b"x-csrf-token",
        b"x-client-ip",
        b"x-cluster-client-ip",
        b"x-correlation-id",
        b"x-forwarded-client-cert",
        b"x-internal-key",
        b"x-mcp-user-id",
        b"x-original-url",
        b"x-real-ip",
        b"x-request-id",
        b"x-rewrite-url",
    }
)
_PLATFORM_PRIVATE_PREFIXES = (
    b"cf-",
    b"x-auth-",
    b"x-amzn-",
    b"x-appengine-",
    b"x-azure-",
    b"x-b3-",
    b"x-canvas-",
    b"x-envoy-",
    b"x-forwarded-",
    b"x-keycloak-",
    b"x-original-",
    b"x-srw-",
    b"x-traefik-",
    b"x-vercel-",
)

_RESPONSE_STRIP = frozenset(
    {
        b"age",
        b"accept-ch",
        b"alt-svc",
        b"cache-control",
        b"clear-site-data",
        b"connection",
        b"content-security-policy",
        b"content-security-policy-report-only",
        b"cross-origin-embedder-policy",
        b"cross-origin-opener-policy",
        b"cross-origin-resource-policy",
        b"critical-ch",
        b"document-policy",
        b"etag",
        b"expires",
        b"expect-ct",
        b"feature-policy",
        b"keep-alive",
        b"last-modified",
        b"link",
        b"nel",
        b"origin-agent-cluster",
        b"origin-trial",
        b"permissions-policy",
        b"pragma",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"referrer-policy",
        b"refresh",
        b"report-to",
        b"reporting-endpoints",
        b"server",
        b"service-worker-allowed",
        b"set-cookie",
        b"set-cookie2",
        b"strict-transport-security",
        b"timing-allow-origin",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
        b"www-authenticate",
        b"x-frame-options",
        b"x-permitted-cross-domain-policies",
        b"x-powered-by",
        b"x-content-type-options",
    }
)


class CanvasProxyError(Exception):
    """A fail-closed, transport-independent Canvas proxy result."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class CanvasProxyLimits:
    """Starting limits for one ordinary HTTP exchange."""

    max_header_bytes: int = 64 * 1024
    max_header_fields: int = 100
    max_query_bytes: int = 16 * 1024
    max_query_fields: int = 256
    max_request_body_bytes: int = 10 * 1024 * 1024
    max_response_body_bytes: int = 100 * 1024 * 1024
    connect_timeout_seconds: float = 5.0
    idle_timeout_seconds: float = 60.0
    exchange_timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        for name in (
            "max_header_bytes",
            "max_header_fields",
            "max_query_bytes",
            "max_query_fields",
            "max_request_body_bytes",
            "max_response_body_bytes",
            "connect_timeout_seconds",
            "idle_timeout_seconds",
            "exchange_timeout_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


def canvas_proxy_limits_from_env() -> CanvasProxyLimits:
    """Load bounded deployment overrides for the ordinary-HTTP gateway."""

    def integer(name: str, default: int, minimum: int, maximum: int) -> int:
        raw = os.getenv(name, str(default)).strip()
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} is outside its safe range")
        return value

    def seconds(name: str, default: float, maximum: float) -> float:
        raw = os.getenv(name, str(default)).strip()
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number") from exc
        if not 0.05 <= value <= maximum:
            raise ValueError(f"{name} is outside its safe range")
        return value

    return CanvasProxyLimits(
        max_header_bytes=integer(
            "CANVAS_VIEWER_MAX_HEADER_BYTES", 64 * 1024, 1024, 1024 * 1024
        ),
        max_header_fields=integer("CANVAS_VIEWER_MAX_HEADER_FIELDS", 100, 10, 1000),
        max_query_bytes=integer(
            "CANVAS_VIEWER_MAX_QUERY_BYTES", 16 * 1024, 256, 1024 * 1024
        ),
        max_query_fields=integer("CANVAS_VIEWER_MAX_QUERY_FIELDS", 256, 1, 4096),
        max_request_body_bytes=integer(
            "CANVAS_VIEWER_MAX_REQUEST_BODY_BYTES",
            10 * 1024 * 1024,
            1,
            100 * 1024 * 1024,
        ),
        max_response_body_bytes=integer(
            "CANVAS_VIEWER_MAX_RESPONSE_BODY_BYTES",
            100 * 1024 * 1024,
            1,
            1024 * 1024 * 1024,
        ),
        connect_timeout_seconds=seconds(
            "CANVAS_VIEWER_CONNECT_TIMEOUT_SECONDS", 5.0, 60.0
        ),
        idle_timeout_seconds=seconds("CANVAS_VIEWER_IDLE_TIMEOUT_SECONDS", 60.0, 300.0),
        exchange_timeout_seconds=seconds(
            "CANVAS_VIEWER_EXCHANGE_TIMEOUT_SECONDS", 300.0, 3600.0
        ),
    )


@dataclass(frozen=True, slots=True)
class CanvasPublicOrigin:
    """One server-allocated browser origin, never an agent-selected URL."""

    host: str
    scheme: str = "https"
    port: int | None = None

    def __post_init__(self) -> None:
        host = self.host
        if (
            not host
            or host != host.lower()
            or len(host) > 253
            or host.endswith(".")
            or ":" in host
            or not host.isascii()
            or any(
                not label
                or len(label) > 63
                or label[0] == "-"
                or label[-1] == "-"
                or not all(char.isalnum() or char == "-" for char in label)
                for label in host.split(".")
            )
        ):
            raise ValueError("Canvas public host must be one canonical DNS name")
        if self.scheme not in {"https", "http"}:
            raise ValueError("Canvas public scheme must be http or https")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("Canvas public port is invalid")

    @property
    def default_port(self) -> int:
        return 443 if self.scheme == "https" else 80

    @property
    def effective_port(self) -> int:
        return self.port or self.default_port

    @property
    def authority(self) -> str:
        if self.port is None or self.port == self.default_port:
            return self.host
        return f"{self.host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.authority}"

    @property
    def websocket_origin(self) -> str:
        scheme = "wss" if self.scheme == "https" else "ws"
        return f"{scheme}://{self.authority}"


@dataclass(frozen=True, slots=True)
class CanvasUpstreamRequest:
    """A fully validated request ready for one h11/direct-channel exchange."""

    method: bytes
    target: bytes
    headers: tuple[tuple[bytes, bytes], ...]
    body_size: int


@dataclass(frozen=True, slots=True)
class ValidatedCanvasRequest:
    """Browser metadata validated before its body is read."""

    method: str
    target: bytes
    forwarded_headers: tuple[tuple[bytes, bytes], ...]
    declared_content_length: int | None
    viewer_cookie: str | None
    origin: CanvasPublicOrigin

    def prepare_upstream(self, body_size: int) -> CanvasUpstreamRequest:
        """Bind the validated metadata to the completely spooled body size."""

        if body_size < 0:
            raise ValueError("body_size cannot be negative")
        if (
            self.declared_content_length is not None
            and self.declared_content_length != body_size
        ):
            raise CanvasProxyError(
                400,
                "canvas_request_framing_invalid",
                "Content-Length does not match the decoded request body",
            )
        authority = self.origin.authority.encode("ascii")
        fixed = [
            (b"host", authority),
            (
                b"forwarded",
                f'proto={self.origin.scheme};host="{self.origin.authority}"'.encode(
                    "ascii"
                ),
            ),
            (b"x-forwarded-proto", self.origin.scheme.encode("ascii")),
            (b"x-forwarded-host", authority),
            (b"x-forwarded-port", str(self.origin.effective_port).encode("ascii")),
            (b"connection", b"close"),
            (b"content-length", str(body_size).encode("ascii")),
        ]
        return CanvasUpstreamRequest(
            method=self.method.encode("ascii"),
            target=self.target,
            headers=tuple([*self.forwarded_headers, *fixed]),
            body_size=body_size,
        )


@dataclass(frozen=True, slots=True)
class CanvasSanitizedResponse:
    status_code: int
    headers: tuple[tuple[bytes, bytes], ...]


def _header_pairs(
    headers: Sequence[tuple[bytes, bytes]], limits: CanvasProxyLimits
) -> list[tuple[bytes, bytes]]:
    if len(headers) > limits.max_header_fields:
        raise CanvasProxyError(
            431, "canvas_headers_too_large", "Too many HTTP header fields"
        )
    total = 2
    result: list[tuple[bytes, bytes]] = []
    for raw_name, raw_value in headers:
        name = bytes(raw_name).lower()
        value = bytes(raw_value)
        total += len(name) + len(value) + 4
        if total > limits.max_header_bytes:
            raise CanvasProxyError(
                431, "canvas_headers_too_large", "HTTP header block is too large"
            )
        if not _TOKEN.fullmatch(name):
            raise CanvasProxyError(
                400, "canvas_headers_invalid", "HTTP header name is invalid"
            )
        if any(byte in {0, 10, 13, 127} or (byte < 32 and byte != 9) for byte in value):
            raise CanvasProxyError(
                400, "canvas_headers_invalid", "HTTP header value is invalid"
            )
        result.append((name, value))
    return result


def _one_header(
    headers: Iterable[tuple[bytes, bytes]], name: bytes, *, required: bool = False
) -> bytes | None:
    values = [value for candidate, value in headers if candidate == name]
    if len(values) > 1 or (required and not values):
        raise CanvasProxyError(
            400,
            "canvas_headers_invalid",
            f"Canvas request requires exactly one {name.decode('ascii')} header",
        )
    return values[0] if values else None


def _canonical_query(raw_query: bytes, limits: CanvasProxyLimits) -> bytes:
    if not raw_query:
        return b""
    if (
        len(raw_query) > limits.max_query_bytes
        or not raw_query.isascii()
        or b"#" in raw_query
    ):
        raise CanvasProxyError(
            414, "canvas_query_invalid", "Canvas request query is invalid"
        )
    fields = raw_query.split(b"&")
    if len(fields) > limits.max_query_fields:
        raise CanvasProxyError(
            414, "canvas_query_invalid", "Canvas request has too many query fields"
        )
    canonical: list[str] = []
    for field in fields:
        raw_key, separator, raw_value = field.partition(b"=")
        if _MALFORMED_PERCENT.search(raw_key) or _MALFORMED_PERCENT.search(raw_value):
            raise CanvasProxyError(
                400, "canvas_query_invalid", "Canvas request query is malformed"
            )
        try:
            key = unquote_to_bytes(raw_key.replace(b"+", b" ")).decode(
                "utf-8", errors="strict"
            )
            value = unquote_to_bytes(raw_value.replace(b"+", b" ")).decode(
                "utf-8", errors="strict"
            )
        except UnicodeDecodeError as exc:
            raise CanvasProxyError(
                400, "canvas_query_invalid", "Canvas request query is not UTF-8"
            ) from exc
        if any(ord(char) < 32 or ord(char) == 127 for char in key + value):
            raise CanvasProxyError(
                400, "canvas_query_invalid", "Canvas request query contains controls"
            )
        # Fail closed on recursively encoded control keys. Other application
        # values may legitimately contain a literal percent escape after one
        # decode, since queries do not participate in routing.
        if _REMAINING_ESCAPE.search(key):
            raise CanvasProxyError(
                400, "canvas_query_invalid", "Canvas request query key is ambiguous"
            )
        normalized_key = key.lower()
        if normalized_key == "_canvas" or normalized_key.startswith("_canvas_"):
            continue
        encoded = quote_plus(key, safe="-._~")
        if separator:
            encoded += "=" + quote_plus(value, safe="-._~")
        canonical.append(encoded)
    return "&".join(canonical).encode("ascii")


def _canonical_path(raw_path: bytes) -> str:
    if not raw_path or not raw_path.isascii():
        raise CanvasProxyError(
            400, "canvas_path_invalid", "Canvas request path is invalid"
        )
    try:
        return canonical_canvas_app_path(raw_path.decode("ascii"))
    except CanvasAppError as exc:
        raise CanvasProxyError(400, "canvas_path_invalid", exc.message) from exc


def _parse_connection_tokens(value: bytes | None) -> set[bytes]:
    if value is None:
        return set()
    result: set[bytes] = set()
    for raw_token in value.split(b","):
        token = raw_token.strip().lower()
        if not token or not _TOKEN.fullmatch(token):
            raise CanvasProxyError(
                400, "canvas_headers_invalid", "Connection header is invalid"
            )
        result.add(token)
    return result


def _viewer_cookie(cookie_header: bytes | None, reserved_name: str) -> str | None:
    if cookie_header is None:
        return None
    try:
        text = cookie_header.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CanvasProxyError(
            400, "canvas_cookie_invalid", "Canvas cookie header is invalid"
        ) from exc
    found: list[str] = []
    for pair in text.split(";"):
        pair = pair.strip()
        if not pair:
            continue
        name, separator, value = pair.partition("=")
        if not separator or not _TOKEN.fullmatch(name.encode("ascii")):
            raise CanvasProxyError(
                400, "canvas_cookie_invalid", "Canvas cookie header is malformed"
            )
        if name == reserved_name:
            found.append(value)
    if len(found) > 1:
        raise CanvasProxyError(
            400,
            "canvas_cookie_invalid",
            "Reserved Canvas viewer cookie is duplicated",
        )
    return found[0] if found else None


def validate_canvas_request(
    *,
    method: str,
    raw_path: bytes | None,
    raw_query: bytes,
    headers: Sequence[tuple[bytes, bytes]],
    public_origin: CanvasPublicOrigin,
    limits: CanvasProxyLimits = CanvasProxyLimits(),
    reserved_cookie_name: str = "__Host-canvas_session",
) -> ValidatedCanvasRequest:
    """Validate the browser boundary without reading or forwarding its body."""

    if method not in ALLOWED_CANVAS_HTTP_METHODS:
        raise CanvasProxyError(
            405, "canvas_method_not_allowed", "HTTP method is not allowed"
        )
    if raw_path is None:
        raise CanvasProxyError(
            503,
            "canvas_raw_path_unavailable",
            "The serving edge did not preserve the raw request path",
        )
    pairs = _header_pairs(headers, limits)
    host = _one_header(pairs, b"host", required=True)
    assert host is not None
    try:
        host_text = host.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CanvasProxyError(
            400, "canvas_host_invalid", "Canvas Host header is invalid"
        ) from exc
    if host_text.lower() != public_origin.authority or host_text != host_text.lower():
        raise CanvasProxyError(
            421, "canvas_host_mismatch", "Canvas Host does not match this origin"
        )

    content_lengths = [value for name, value in pairs if name == b"content-length"]
    transfer_encodings = [
        value for name, value in pairs if name == b"transfer-encoding"
    ]
    if (
        len(content_lengths) > 1
        or len(transfer_encodings) > 1
        or (content_lengths and transfer_encodings)
        or (transfer_encodings and transfer_encodings[0].strip().lower() != b"chunked")
    ):
        raise CanvasProxyError(
            400,
            "canvas_request_framing_invalid",
            "Ambiguous client HTTP framing is not accepted",
        )
    if any(name == b"trailer" for name, _ in pairs):
        raise CanvasProxyError(
            400,
            "canvas_request_framing_invalid",
            "Request trailers are not accepted",
        )
    declared_length: int | None = None
    if content_lengths:
        raw_length = content_lengths[0]
        if not raw_length.isdigit() or len(raw_length) > 20:
            raise CanvasProxyError(
                400,
                "canvas_request_framing_invalid",
                "Content-Length is invalid",
            )
        declared_length = int(raw_length)
        if declared_length > limits.max_request_body_bytes:
            raise CanvasProxyError(
                413, "canvas_request_too_large", "Canvas request body is too large"
            )

    connection = _one_header(pairs, b"connection")
    connection_tokens = _parse_connection_tokens(connection)
    if b"upgrade" in connection_tokens or any(
        name == b"upgrade" or name.startswith(b"sec-websocket-") for name, _ in pairs
    ):
        raise CanvasProxyError(
            426,
            "canvas_websocket_unsupported",
            "WebSocket preview is not available in this Canvas slice",
        )

    origin_value = _one_header(pairs, b"origin")
    if origin_value is None:
        if method not in {"GET", "HEAD"}:
            raise CanvasProxyError(
                403,
                "canvas_origin_required",
                "Unsafe Canvas requests require an exact same-origin Origin",
            )
    else:
        try:
            request_origin = origin_value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise CanvasProxyError(
                403, "canvas_origin_mismatch", "Canvas Origin is invalid"
            ) from exc
        if request_origin != public_origin.origin:
            raise CanvasProxyError(
                403,
                "canvas_origin_mismatch",
                "Cross-origin Canvas requests are not allowed",
            )

    cookie = _one_header(pairs, b"cookie")
    viewer_cookie = _viewer_cookie(cookie, reserved_cookie_name)
    canonical_path = _canonical_path(raw_path)
    canonical_query = _canonical_query(raw_query, limits)
    target = canonical_path.encode("ascii")
    if canonical_query:
        target += b"?" + canonical_query

    forwarded: list[tuple[bytes, bytes]] = []
    for name, value in pairs:
        if name in {
            b"host",
            b"origin",
            b"cookie",
            b"content-length",
        }:
            continue
        if (
            name in _HOP_BY_HOP
            or name in _PLATFORM_PRIVATE
            or name in connection_tokens
            or name.startswith(_PLATFORM_PRIVATE_PREFIXES)
        ):
            continue
        forwarded.append((name, value))
    if origin_value is not None:
        forwarded.append((b"origin", public_origin.origin.encode("ascii")))

    return ValidatedCanvasRequest(
        method=method,
        target=target,
        forwarded_headers=tuple(forwarded),
        declared_content_length=declared_length,
        viewer_cookie=viewer_cookie,
        origin=public_origin,
    )


def rewrite_canvas_location(
    value: bytes,
    *,
    current_path: str,
    public_origin: CanvasPublicOrigin,
    entry_port: int,
    limits: CanvasProxyLimits = CanvasProxyLimits(),
) -> bytes:
    """Normalize a safe same-origin/entry-loopback redirect.

    External, ambiguous, custom-scheme, and structurally unsafe navigation is
    rejected. A later trusted-parent confirmation flow can handle selected
    external HTTPS targets without weakening this ordinary proxy boundary.
    """

    if len(value) > 4096:
        raise CanvasProxyError(
            409, "canvas_navigation_blocked", "Canvas redirect is too large"
        )
    try:
        text = value.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CanvasProxyError(
            409, "canvas_navigation_blocked", "Canvas redirect is invalid"
        ) from exc
    if (
        not text
        or "\\" in text
        or text.startswith("//")
        or any(ord(char) < 32 or ord(char) == 127 for char in text)
    ):
        raise CanvasProxyError(
            409, "canvas_navigation_blocked", "Canvas redirect is unsafe"
        )
    try:
        parsed = urlsplit(text)
        parsed_port = parsed.port
    except ValueError as exc:
        raise CanvasProxyError(
            409, "canvas_navigation_blocked", "Canvas redirect is malformed"
        ) from exc
    if parsed.username is not None or parsed.password is not None:
        raise CanvasProxyError(
            409, "canvas_navigation_blocked", "Canvas redirect has credentials"
        )

    absolute = bool(parsed.scheme or parsed.netloc)
    if absolute:
        hostname = (parsed.hostname or "").lower()
        effective_port = parsed_port or (443 if parsed.scheme == "https" else 80)
        same_origin = (
            parsed.scheme == public_origin.scheme
            and hostname == public_origin.host
            and effective_port == public_origin.effective_port
        )
        entry_loopback = (
            parsed.scheme == "http"
            and hostname in {"localhost", "127.0.0.1"}
            and effective_port == entry_port
        )
        if not same_origin and not entry_loopback:
            raise CanvasProxyError(
                409,
                "canvas_navigation_blocked",
                "Canvas redirect leaves the allocated origin",
            )
        candidate_path = parsed.path or "/"
    else:
        if parsed.path.startswith("/"):
            candidate_path = parsed.path
        elif parsed.path:
            base = (
                current_path
                if current_path.endswith("/")
                else current_path.rsplit("/", 1)[0] + "/"
            )
            candidate_path = base + parsed.path
        else:
            candidate_path = current_path

    path = _canonical_path(candidate_path.encode("ascii"))
    query = _canonical_query(parsed.query.encode("ascii"), limits)
    fragment = parsed.fragment
    if any(ord(char) < 32 or ord(char) == 127 for char in fragment):
        raise CanvasProxyError(
            409, "canvas_navigation_blocked", "Canvas redirect fragment is invalid"
        )
    result = public_origin.origin + path
    if query:
        result += "?" + query.decode("ascii")
    if fragment:
        result += "#" + quote(fragment, safe="-._~!$&'()*+,;=:@/?")
    return result.encode("ascii")


def _gateway_csp(
    public_origin: CanvasPublicOrigin, cockpit_origins: Sequence[str]
) -> bytes:
    if not cockpit_origins:
        raise ValueError("At least one trusted Cockpit origin is required")
    ancestors: list[str] = []
    for raw in cockpit_origins:
        if not raw.isascii() or any(
            char.isspace() or char in {"'", '"', ";"} for char in raw
        ):
            raise ValueError("Cockpit frame ancestor must be one exact origin")
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Cockpit frame ancestor must be one exact origin")
        try:
            ancestor = CanvasPublicOrigin(
                parsed.hostname,
                scheme=parsed.scheme,
                port=parsed.port,
            ).origin
        except (TypeError, ValueError) as exc:
            raise ValueError("Cockpit frame ancestor must be one exact origin") from exc
        if raw.rstrip("/") != ancestor:
            raise ValueError("Cockpit frame ancestor must be canonical")
        ancestors.append(ancestor)
    policy = (
        "default-src 'self' data: blob:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self' data:; "
        f"connect-src 'self' {public_origin.websocket_origin}; "
        # An app framing its own pages is a normal shape (galleries, previews,
        # doc viewers) and reaches nothing new: a nested same-origin document
        # is served by this gateway, through this session, under this same
        # policy and sandbox. It takes both halves — the parent's frame-src for
        # the nested load, and 'self' in frame-ancestors because the app origin
        # then sits in that nested document's ancestor chain. Admitting only
        # frame-src leaves the child refused. Anti-framing is intact: browsers
        # check every ancestor, so the outermost frame must still be a Cockpit
        # origin and a hostile page gains nothing by chaining through one.
        "frame-src 'self' blob:; object-src 'none'; worker-src 'none'; "
        "base-uri 'self'; form-action 'self'; "
        "sandbox allow-scripts allow-same-origin allow-forms; "
        f"frame-ancestors 'self' {' '.join(ancestors)}"
    )
    return policy.encode("ascii")


def sanitize_canvas_response_headers(
    *,
    status_code: int,
    headers: Sequence[tuple[bytes, bytes]],
    request_method: str,
    request_path: str,
    public_origin: CanvasPublicOrigin,
    cockpit_origins: Sequence[str],
    entry_port: int,
    limits: CanvasProxyLimits = CanvasProxyLimits(),
) -> CanvasSanitizedResponse:
    """Apply the non-relaxable cookie-free live-response policy."""

    if status_code == 101:
        raise CanvasProxyError(
            502,
            "canvas_protocol_switch_rejected",
            "Unexpected upstream protocol switch",
        )
    try:
        pairs = _header_pairs(headers, limits)
    except CanvasProxyError as exc:
        code = (
            "canvas_upstream_headers_too_large"
            if exc.code == "canvas_headers_too_large"
            else "canvas_upstream_invalid"
        )
        raise CanvasProxyError(
            502, code, "Upstream response headers are invalid"
        ) from exc
    content_types = [value for name, value in pairs if name == b"content-type"]
    if len(content_types) > 1:
        raise CanvasProxyError(
            502, "canvas_upstream_invalid", "Upstream Content-Type is ambiguous"
        )
    if content_types:
        media_type = content_types[0].split(b";", 1)[0].strip().lower()
        if media_type in {b"text/event-stream", b"multipart/x-mixed-replace"}:
            raise CanvasProxyError(
                415,
                "canvas_streaming_unsupported",
                "Streaming preview is not available in this Canvas slice",
            )

    locations = [value for name, value in pairs if name == b"location"]
    if len(locations) > 1:
        raise CanvasProxyError(
            502, "canvas_upstream_invalid", "Upstream redirect is ambiguous"
        )

    try:
        connection = _one_header(pairs, b"connection")
        connection_tokens = _parse_connection_tokens(connection)
    except CanvasProxyError as exc:
        raise CanvasProxyError(
            502, "canvas_upstream_invalid", "Upstream Connection header is invalid"
        ) from exc
    clean: list[tuple[bytes, bytes]] = []
    for name, value in pairs:
        if name == b"location":
            continue
        if (
            name in _RESPONSE_STRIP
            or name in _HOP_BY_HOP
            or name in _PLATFORM_PRIVATE
            or name in connection_tokens
            or name.startswith(b"access-control-")
            or name.startswith(_PLATFORM_PRIVATE_PREFIXES)
        ):
            continue
        # Downstream framing is owned by the trusted ASGI server. Range remains
        # a real upstream method and its semantic headers are preserved. HEAD's
        # validated representation length is restored separately below.
        if name == b"content-length":
            continue
        clean.append((name, value))

    if locations:
        clean.append(
            (
                b"location",
                rewrite_canvas_location(
                    locations[0],
                    current_path=request_path,
                    public_origin=public_origin,
                    entry_port=entry_port,
                    limits=limits,
                ),
            )
        )

    if request_method == "HEAD":
        lengths = [value for name, value in pairs if name == b"content-length"]
        if lengths:
            clean.append((b"content-length", lengths[0]))

    clean.extend(
        [
            (b"content-security-policy", _gateway_csp(public_origin, cockpit_origins)),
            (
                b"permissions-policy",
                b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
                b"serial=(), bluetooth=(), hid=(), midi=(), display-capture=(), "
                b"clipboard-read=(), clipboard-write=(), fullscreen=()",
            ),
            (b"referrer-policy", b"no-referrer"),
            (b"x-content-type-options", b"nosniff"),
            (b"cache-control", b"private, no-store"),
        ]
    )
    # Include gateway-owned fields in the same configured response-header
    # budget instead of treating the policy envelope as free overhead.
    try:
        _header_pairs(clean, limits)
    except CanvasProxyError as exc:
        raise CanvasProxyError(
            502,
            "canvas_upstream_headers_too_large",
            "Sanitized upstream response headers exceed the Canvas limit",
        ) from exc
    return CanvasSanitizedResponse(status_code=status_code, headers=tuple(clean))


__all__ = [
    "ALLOWED_CANVAS_HTTP_METHODS",
    "CanvasProxyError",
    "CanvasProxyLimits",
    "CanvasPublicOrigin",
    "CanvasSanitizedResponse",
    "CanvasUpstreamRequest",
    "ValidatedCanvasRequest",
    "canvas_proxy_limits_from_env",
    "rewrite_canvas_location",
    "sanitize_canvas_response_headers",
    "validate_canvas_request",
]
