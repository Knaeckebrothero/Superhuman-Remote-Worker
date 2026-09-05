"""CSRF defense for the cookie BFF.

Three layered checks on non-safe methods:

1. ``Sec-Fetch-Site`` — reject if the request is ``cross-site``. This header
   is unforgeable from JavaScript and is sent by every browser shipping
   since March 2023. OWASP's Dec 2025 CSRF Cheat Sheet upgrades it from
   "fallback" to a primary mechanism.
2. ``X-CSRF: 1`` — a custom header set by the cockpit's HTTP interceptor.
   Non-safelisted custom headers force a CORS preflight, which an attacker
   on another origin cannot satisfy. This is the Duende BFF pattern.
3. ``Origin`` allowlist — fallback for clients that for some reason omit
   ``Sec-Fetch-Site`` (very few; mostly buggy proxies). Only enforced when
   ``Sec-Fetch-Site`` is absent.

Requests that DON'T present the ``srw_session`` cookie skip CSRF entirely.
The cookie *is* the CSRF vector — without it, there is no browser-mediated
session to forge against:
- Bearer-authenticated callers (PATs, MCP tokens, transitional JWT).
- ``X-Internal-Key`` callers (MCP server pod).
- In-cluster agent → orchestrator HTTP traffic (no cookie, no auth — the
  trust boundary is the cluster network; see ``/api/agents/register`` and
  the other "no auth, agent-facing" routes in main.py).

Exempt paths (kept for explicitness even though they would skip via the
no-cookie rule):
- ``/auth/backchannel-logout`` — Keycloak posts a signed JWT here; no
  browser context, no cookie. Signature verification stands in for CSRF.
- ``/api/internal/*`` — MCP-mediated trust path with its own header check.
- ``/api/health`` — unauthenticated health probe.
"""

from __future__ import annotations

import json
import ipaddress
import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Static exempt paths — exact match.
_EXEMPT_EXACT = frozenset({"/api/health"})

# Exempt prefixes.
_EXEMPT_PREFIXES = (
    "/auth/backchannel-logout",
    "/api/internal/",
)


def _normalize_browser_origin(value: str) -> str | None:
    """Return one canonical HTTP(S) origin, rejecting URL-shaped variants."""

    if not isinstance(value, str):
        return None
    value = value.strip()
    if (
        not value
        or value.lower() == "null"
        or "\\" in value
        or any(ord(char) < 33 or ord(char) == 127 for char in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
    ):
        return None
    hostname = parsed.hostname
    if not hostname or hostname.endswith(".") or "%" in hostname:
        return None

    bracketed = False
    try:
        address = ipaddress.ip_address(hostname)
        host = address.compressed.lower()
        bracketed = address.version == 6
    except ValueError:
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return None
        if len(host) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            return None

    if port == (80 if scheme == "http" else 443):
        port = None
    authority = f"[{host}]" if bracketed else host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def allowed_browser_origins() -> frozenset[str]:
    """Return the normalized HTTP CSRF and browser-WebSocket origin authority."""

    statics = {
        "http://localhost:4200",
        "http://127.0.0.1:4200",
        "http://localhost:4000",
        "http://127.0.0.1:4000",
    }
    from_env = {
        normalized
        for item in os.environ.get("CORS_ORIGINS", "").split(",")
        if (normalized := _normalize_browser_origin(item)) is not None
    }
    return frozenset(statics | from_env)


def websocket_origin_allowed(headers: Any) -> bool:
    """Require exactly one non-null Origin in the shared browser allowlist."""

    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        values = list(getlist("origin"))
    else:
        value = headers.get("origin") if hasattr(headers, "get") else None
        values = [] if value is None else [value]
    if len(values) != 1:
        return False
    normalized = _normalize_browser_origin(values[0])
    return normalized is not None and normalized in allowed_browser_origins()


class CSRFMiddleware:
    """ASGI middleware implementing the three-layer CSRF check.

    Implemented as a class (not the ``@app.middleware('http')`` shortcut)
    so we can short-circuit before reaching Starlette's exception layer,
    and so the exempt-path check happens before any body is consumed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope["method"]
        if method in _SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        path = scope["path"]
        if path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        request = Request(scope)

        # No session cookie → no CSRF vector. Browser cross-site requests
        # ride on the user's cookie; without one, an attacker can't impersonate
        # the user via a forged form. This also covers in-cluster agent
        # traffic and any other non-browser client (CI, curl, n8n) that
        # legitimately needs to POST without going through the BFF.
        if "srw_session" not in request.cookies:
            await self.app(scope, receive, send)
            return

        # Bearer-authenticated requests skip CSRF entirely. PATs, MCP tokens,
        # and transitional JWT-from-cockpit all match here. (Reached only on
        # the rare hybrid request that carries BOTH a cookie and a Bearer —
        # we trust the Bearer in that case.)
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            await self.app(scope, receive, send)
            return

        # MCP-internal trust path (X-Internal-Key + X-MCP-User-Id) also
        # skips CSRF — same rationale as bearer.
        if request.headers.get("x-internal-key"):
            await self.app(scope, receive, send)
            return

        # Layer 1: Sec-Fetch-Site.
        sfs = request.headers.get("sec-fetch-site")
        if sfs == "cross-site":
            await _send_403(send, "csrf:cross-site")
            return

        # Layer 2: custom header preflight forcer.
        if not request.headers.get("x-csrf"):
            await _send_403(send, "csrf:missing-header")
            return

        # Layer 3: Origin allowlist — only consulted when sec-fetch-site is
        # absent (otherwise it adds nothing). Same-origin browsers may omit
        # Origin entirely on some POSTs; we only reject when it's present
        # and not in the allowlist.
        if sfs is None:
            origins = request.headers.getlist("origin")
            if origins and (
                len(origins) != 1
                or (normalized := _normalize_browser_origin(origins[0])) is None
                or normalized not in allowed_browser_origins()
            ):
                await _send_403(send, "csrf:bad-origin")
                return

        await self.app(scope, receive, send)


async def _send_403(send: Send, reason: str) -> None:
    """Emit a JSON 403 directly via raw ASGI events.

    We log at warning so legit failures show up in dashboards; we never
    echo the reason back to the client (would help an attacker tune their
    probe).
    """
    logger.warning("CSRF rejection: %s", reason)
    body = json.dumps({"detail": "CSRF check failed"}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


__all__ = [
    "CSRFMiddleware",
    "allowed_browser_origins",
    "websocket_origin_allowed",
]
