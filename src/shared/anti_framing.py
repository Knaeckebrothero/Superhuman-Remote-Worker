"""Response-level anti-framing protection for trusted platform origins.

Dynamic Canvas live applications run with ``allow-same-origin`` on a separate,
untrusted origin.  A hostile app can navigate its own frame, so every document
served from the trusted Cockpit/BFF origin must refuse embedding.  This pure
ASGI middleware applies that invariant to the orchestrator side of the shared
origin, including redirects and error responses produced inside the application
middleware stack.

An additional CSP field is appended instead of replacing an existing policy.
Browsers enforce multiple CSP policies cumulatively, which lets a response keep
its route-specific restrictions while the independent ``frame-ancestors``
policy closes the framing boundary.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_CSP_HEADER = b"content-security-policy"
_FRAME_ANCESTORS_NONE = b"frame-ancestors 'none'"
_FRAME_ANCESTORS_SELF = b"frame-ancestors 'self'"
_X_FRAME_OPTIONS = b"x-frame-options"
_X_FRAME_OPTIONS_DENY = b"DENY"
_X_FRAME_OPTIONS_SAMEORIGIN = b"SAMEORIGIN"
_INTERNAL_ERROR_BODY = b'{"detail":"Internal Server Error"}'


def _normalize_host_authority(value: str) -> str | None:
    """Return a canonical Host-header authority, or ``None`` when invalid."""

    try:
        parsed = urlsplit(f"//{value}")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        hostname = parsed.hostname
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    if not hostname:
        return None
    try:
        hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if not hostname:
        return None
    host_literal = f"[{hostname}]" if ":" in hostname else hostname
    return f"{host_literal}:{port}" if port is not None else host_literal


class TrustedParentAntiFramingMiddleware:
    """Deny framing for HTTP responses from a trusted parent app.

    ``same_origin_frameable_path_authorities`` is a narrow compatibility valve
    for an intentionally frame-based application on a *different* public
    origin. A matching response receives ``frame-ancestors 'self'`` and
    ``SAMEORIGIN`` rather than becoming unrestricted. Both its non-root path
    prefix and exact request authority must match; the same path on the Cockpit
    authority therefore remains denied.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        same_origin_frameable_path_authorities: Mapping[str, Collection[str]]
        | None = None,
    ) -> None:
        self.app = app
        policies = []
        for prefix, authorities in (
            same_origin_frameable_path_authorities or {}
        ).items():
            if not prefix.startswith("/") or prefix == "/" or not prefix.endswith("/"):
                raise ValueError(
                    "same-origin frameable prefixes must be non-root absolute "
                    "path prefixes ending in '/'"
                )
            normalized = frozenset(
                authority
                for raw_authority in authorities
                if (authority := _normalize_host_authority(raw_authority)) is not None
            )
            if not normalized:
                raise ValueError(
                    "same-origin frameable prefixes require at least one valid authority"
                )
            policies.append((prefix, normalized))
        self._same_origin_frameable_path_authorities = tuple(policies)

    def _uses_same_origin_framing(self, scope: Scope) -> bool:
        if not self._same_origin_frameable_path_authorities:
            return False
        host_values = [
            value for name, value in scope.get("headers", []) if name.lower() == b"host"
        ]
        if len(host_values) != 1:
            return False
        try:
            raw_authority = host_values[0].decode("ascii")
        except UnicodeDecodeError:
            return False
        authority = _normalize_host_authority(raw_authority)
        if authority is None:
            return False
        path = scope.get("path", "")
        return any(
            path.startswith(prefix) and authority in authorities
            for prefix, authorities in self._same_origin_frameable_path_authorities
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        same_origin_framing = self._uses_same_origin_framing(scope)
        response_started = False

        async def send_with_anti_framing(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                original_headers = message.get("headers", [])
                existing_xfo_deny = any(
                    name.lower() == _X_FRAME_OPTIONS
                    and value.strip().lower() == _X_FRAME_OPTIONS_DENY.lower()
                    for name, value in original_headers
                )

                # Replace every legacy X-Frame-Options field so intermediaries
                # never receive conflicting DENY/SAMEORIGIN/ALLOW-FROM values.
                headers = [
                    (name, value)
                    for name, value in original_headers
                    if name.lower() != _X_FRAME_OPTIONS
                ]
                xfo = (
                    _X_FRAME_OPTIONS_DENY
                    if not same_origin_framing or existing_xfo_deny
                    else _X_FRAME_OPTIONS_SAMEORIGIN
                )
                headers.append((_X_FRAME_OPTIONS, xfo))

                # Do not merge or replace a route-owned CSP.  A second enforced
                # policy composes restrictively and cannot weaken directives on
                # sensitive HTML/file responses.
                frame_ancestors = (
                    _FRAME_ANCESTORS_SELF
                    if same_origin_framing
                    else _FRAME_ANCESTORS_NONE
                )
                if not any(
                    name.lower() == _CSP_HEADER
                    and value.strip().lower()
                    in {
                        frame_ancestors,
                        # An upstream 'none' is already stricter than the IDE's
                        # same-origin compatibility policy and must stay strict.
                        _FRAME_ANCESTORS_NONE,
                    }
                    for name, value in headers
                ):
                    headers.append((_CSP_HEADER, frame_ancestors))
                message["headers"] = headers

            await send(message)

        try:
            await self.app(scope, receive, send_with_anti_framing)
        except Exception:
            # Starlette's ServerErrorMiddleware sits outside user middleware.
            # Without this start message its generated 500 would bypass the
            # trusted-parent boundary. Send the same non-debug body first, then
            # re-raise so the outer server/test client can still log the error.
            if not response_started:
                await send_with_anti_framing(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (
                                b"content-length",
                                str(len(_INTERNAL_ERROR_BODY)).encode(),
                            ),
                        ],
                    }
                )
                await send_with_anti_framing(
                    {
                        "type": "http.response.body",
                        "body": _INTERNAL_ERROR_BODY,
                    }
                )
            raise
