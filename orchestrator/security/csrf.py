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
import logging
import os

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


def _allowed_origins() -> set[str]:
    """Origins permitted to issue state-changing requests with the session
    cookie. Mirrors the CORS allowlist; same env knob.
    """
    statics = {
        "http://localhost:4200",
        "http://127.0.0.1:4200",
        "http://localhost:4000",
        "http://127.0.0.1:4000",
    }
    from_env = {o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o}
    return statics | from_env


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
            origin = request.headers.get("origin")
            if origin and origin not in _allowed_origins():
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
