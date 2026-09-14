"""AI subscriptions: proxy management client, login sessions, inventory.

SRW owns the connection UI, the catalog, client-protocol selection and access
policy. CLIProxyAPI owns provider authentication, upstream account selection
and API schema conversion. This module is the boundary between the two: every
call SRW makes to the proxy's ``/v0/management`` surface goes through here, and
nothing in it ever hands a secret to the browser.

Three things it deliberately does **not** do:

* It does not probe whether a provider is supported by calling its auth-url
  handler. At v7.2.110 those handlers *register a real OAuth session* (and bind
  a callback port) before returning, so "asking" would leave sessions behind.
  Support comes from the static registry in ``subscription_providers``.
* It does not treat a callback — or a permissive poll response — as proof of
  connection. Upstream's ``get-auth-status`` answers ``{"status":"ok"}`` for an
  *empty* state and for a session it has already forgotten, so success is
  confirmed against the credential inventory, not against the poll alone.
* It does not send a non-Codex credential to ChatGPT's usage endpoint. The
  usage reader is scoped to a confirmed ``codex`` credential and its fixed
  destination; every other provider reports usage as unavailable.

Design: knowledge-base/knowledge/features/subscription_proxy.md §5, §7.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import parse_qs, urlparse

import httpx

from orchestrator.services.subscription_providers import (
    LOGIN_FLOW_BROWSER,
    LOGIN_FLOW_DEVICE,
    SUBSCRIPTION_PROVIDERS,
    SubscriptionProvider,
    get_provider,
    provider_for_channel,
)
from shared.subscription_routing import normalize_channel

logger = logging.getLogger(__name__)

_DEFAULT_PROXY_URL = "http://localhost:8317"

# Upstream keeps an OAuth session for 30 minutes (``oauthSessionTTL``); SRW's
# own session must not outlive it, or we would keep polling a state the proxy
# has already forgotten and render "waiting" forever.
LOGIN_SESSION_TTL = timedelta(minutes=30)

# After upstream reports the flow complete we still require the credential to
# actually appear in the inventory. This is the window we allow for the proxy
# to finish persisting + registering it before calling the login failed.
LOGIN_RECONCILE_GRACE = timedelta(seconds=20)

# Account inventory is read on nearly every settings render and on every
# discovery pass. A short TTL collapses a burst of UI refreshes into one
# upstream call without ever showing a stale connect/disconnect (both
# invalidate explicitly).
ACCOUNT_CACHE_TTL_SECONDS = 10.0

# Bound the per-account model fan-out during discovery.
_INVENTORY_CONCURRENCY = 4


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SubscriptionProxyError(RuntimeError):
    """A management call failed. ``status`` is the HTTP status SRW should map to."""

    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class SubscriptionProxyUnreachable(SubscriptionProxyError):
    """The proxy could not be contacted at all (disabled, down, wrong URL)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def proxy_base_url() -> str:
    """Management/inference root of the configured proxy (no trailing slash).

    ``SUBSCRIPTION_PROXY_URL`` is the forward-looking name; ``CODEX_PROXY_URL``
    is what every deployed Secret and values file sets today and stays
    authoritative for existing installs (feature doc §8.5).
    """
    url = (
        os.getenv("SUBSCRIPTION_PROXY_URL")
        or os.getenv("CODEX_PROXY_URL")
        or _DEFAULT_PROXY_URL
    )
    return url.rstrip("/")


def _management_key() -> str:
    """Credential for ``/v0/management/*``. Never leaves the orchestrator."""
    return (
        os.getenv("SUBSCRIPTION_PROXY_MANAGEMENT_KEY")
        or os.getenv("CODEX_MANAGEMENT_KEY")
        or ""
    )


# Redact anything that looks like a bearer token, an authorization code or an
# OAuth state out of an upstream error body before it can reach a log line or
# an HTTP response. Upstream echoes request context in some error paths.
_SECRETISH = re.compile(
    r"(?i)\b((?:access|refresh|id)_token|authorization|bearer|api[-_]?key"
    r"|client_secret|code|state)\b[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9._~+/=-]{8,}[\"']?"
)


def sanitize_upstream_detail(text: str | None, *, limit: int = 200) -> str:
    """Trim + redact an upstream body so it is safe to surface and to log."""
    if not text:
        return ""
    cleaned = _SECRETISH.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


async def management_request(
    method: str,
    path: str,
    *,
    timeout: float = 10.0,
    **kwargs: Any,
) -> httpx.Response:
    """Call the proxy's management API. Raises :class:`SubscriptionProxyError`.

    The management key is attached here and nowhere else; callers never see it
    and it is never included in an error message.
    """
    base = proxy_base_url()
    headers = dict(kwargs.pop("headers", None) or {})
    key = _management_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method, f"{base}{path}", headers=headers, **kwargs
            )
    except httpx.RequestError as exc:
        raise SubscriptionProxyUnreachable(
            f"Subscription proxy unreachable at {base}: {type(exc).__name__}"
        ) from exc

    if response.status_code >= 400:
        raise SubscriptionProxyError(
            f"Subscription proxy returned {response.status_code}: "
            f"{sanitize_upstream_detail(response.text)}"
        )
    return response


def _json_or_empty(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {}


# ---------------------------------------------------------------------------
# Account inventory
# ---------------------------------------------------------------------------

# Opaque, reversible account handle. The proxy's own ``auth_index`` is a
# *runtime* position and is re-assigned across restarts, so it can never be a
# durable account id. The credential's ``name`` (its file name) is stable, but
# contains '@' and '.' and must not be pasted into a URL path raw.


def encode_account_id(name: str) -> str:
    return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii").rstrip("=")


def decode_account_id(account_id: str) -> str:
    padded = account_id + "=" * (-len(account_id) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise SubscriptionProxyError(
            "Unknown subscription account id", status=404
        ) from exc


@dataclass(frozen=True)
class SubscriptionAccount:
    """One connected credential, as SRW presents it (no secrets)."""

    account_id: str
    name: str
    channel: str | None
    provider_key: str | None
    label: str | None
    email: str | None
    account_type: str | None
    state: str
    state_detail: str | None
    disabled: bool
    unavailable: bool
    next_retry_after: str | None
    updated_at: str | None
    last_refresh: str | None
    #: Runtime position reported by the proxy. Diagnostic only — never an id.
    auth_index: str | None

    @property
    def healthy(self) -> bool:
        return self.state == "connected"

    def to_public(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "provider": self.provider_key,
            "channel": self.channel,
            "label": self.label,
            "email": self.email,
            "account_type": self.account_type,
            "state": self.state,
            "state_detail": self.state_detail,
            "next_retry_after": self.next_retry_after,
            "updated_at": self.updated_at,
            "last_refresh": self.last_refresh,
            # Shared installation-wide connection managed by admins. Private
            # per-user subscriptions are deliberately out of scope for this
            # release (feature doc §10.1) — the field exists so the UI states
            # the scope instead of leaving it ambiguous.
            "scope": "installation",
        }


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _account_state(entry: Mapping[str, Any]) -> tuple[str, str | None]:
    """Map an upstream auth-file entry to an SRW connection state.

    Distinguishes only what upstream actually reports (``sdk/cliproxy/auth/
    status.go``: unknown/active/pending/refreshing/error/disabled, plus the
    ``disabled``/``unavailable``/``next_retry_after`` flags). We never invent
    an "expired" state the proxy has not claimed.
    """
    detail = _as_text(entry.get("status_message"))
    if _as_bool(entry.get("disabled")) or entry.get("status") == "disabled":
        return "disabled", detail
    status = (entry.get("status") or "").strip().lower()
    if _as_bool(entry.get("unavailable")):
        # A cooldown carries a retry deadline; a hard failure does not.
        return ("cooldown" if entry.get("next_retry_after") else "error"), detail
    if status == "error":
        return "error", detail
    if status in {"pending", "refreshing"}:
        return status, detail
    if status == "active":
        return "connected", detail
    return "unknown", detail


def account_from_entry(entry: Mapping[str, Any]) -> SubscriptionAccount | None:
    """Project one sanitized account out of an upstream auth-file entry.

    Whitelist, not blacklist: upstream entries also carry the on-disk ``path``
    and decoded ``id_token`` claims, neither of which belongs in a browser
    payload.
    """
    name = _as_text(entry.get("name")) or _as_text(entry.get("id"))
    if not name:
        return None
    channel = normalize_channel(
        _as_text(entry.get("provider")) or _as_text(entry.get("type"))
    )
    provider = provider_for_channel(channel)
    state, detail = _account_state(entry)
    return SubscriptionAccount(
        account_id=encode_account_id(name),
        name=name,
        channel=channel,
        provider_key=provider.key if provider else None,
        label=_as_text(entry.get("label")),
        email=_as_text(entry.get("email")),
        account_type=_as_text(entry.get("account_type")),
        state=state,
        state_detail=detail,
        disabled=_as_bool(entry.get("disabled")),
        unavailable=_as_bool(entry.get("unavailable")),
        next_retry_after=_as_text(entry.get("next_retry_after")),
        updated_at=_as_text(entry.get("updated_at")) or _as_text(entry.get("modtime")),
        last_refresh=_as_text(entry.get("last_refresh")),
        auth_index=_as_text(entry.get("auth_index")),
    )


def _entries_of(payload: Any) -> list[Mapping[str, Any]]:
    """Upstream returns ``{"files": [...]}``; older builds returned a bare list."""
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, Mapping):
        raw = payload.get("files") or []
    else:
        raw = []
    return [item for item in raw if isinstance(item, Mapping)]


_accounts_cache: tuple[float, list[SubscriptionAccount]] | None = None
_accounts_cache_lock = asyncio.Lock()


def invalidate_account_cache() -> None:
    """Drop the cached inventory (after a connect, disconnect or import)."""
    global _accounts_cache
    _accounts_cache = None


async def list_accounts(*, use_cache: bool = True) -> list[SubscriptionAccount]:
    """Sanitized inventory of every credential the proxy holds.

    Raises :class:`SubscriptionProxyError` when the proxy cannot be read — an
    empty list means "the proxy answered and holds nothing", which callers must
    be able to tell apart from "we could not ask".
    """
    global _accounts_cache
    if use_cache:
        cached = _accounts_cache
        if cached and (time.monotonic() - cached[0]) < ACCOUNT_CACHE_TTL_SECONDS:
            return list(cached[1])

    async with _accounts_cache_lock:
        cached = _accounts_cache
        if (
            use_cache
            and cached
            and (time.monotonic() - cached[0]) < ACCOUNT_CACHE_TTL_SECONDS
        ):
            return list(cached[1])
        response = await management_request("GET", "/v0/management/auth-files")
        accounts = [
            account
            for account in (
                account_from_entry(entry)
                for entry in _entries_of(_json_or_empty(response))
            )
            if account is not None
        ]
        _accounts_cache = (time.monotonic(), accounts)
        return list(accounts)


async def delete_account(account_id: str) -> str:
    """Disconnect one account. Returns the credential name that was removed."""
    name = decode_account_id(account_id)
    accounts = await list_accounts(use_cache=False)
    if not any(a.name == name for a in accounts):
        raise SubscriptionProxyError("Unknown subscription account", status=404)
    await management_request(
        "DELETE", "/v0/management/auth-files", params={"name": name}
    )
    invalidate_account_cache()
    return name


def accounts_for_provider(
    accounts: Iterable[SubscriptionAccount], provider: SubscriptionProvider
) -> list[SubscriptionAccount]:
    channels = set(provider.channels)
    return [a for a in accounts if a.channel in channels]


# ---------------------------------------------------------------------------
# Proxy status
# ---------------------------------------------------------------------------


async def proxy_status() -> dict[str, Any]:
    """Reachability + connected accounts + advertised model count.

    Reachability and *authentication* are reported separately, so the UI can
    say "the proxy is off" rather than "you are signed out".
    """
    base = proxy_base_url()
    try:
        accounts = await list_accounts(use_cache=False)
    except SubscriptionProxyError as exc:
        return {
            "reachable": False,
            "proxy_url": base,
            "error": exc.message,
            "connected": False,
            "accounts": [],
            "model_count": 0,
            "providers": [
                _provider_public(p, connected=0) for p in SUBSCRIPTION_PROVIDERS
            ],
        }

    model_count = 0
    try:
        payload = _json_or_empty(await management_request("GET", "/v1/models"))
        data = payload.get("data") if isinstance(payload, Mapping) else None
        model_count = len(data or [])
    except SubscriptionProxyError:
        # An inventory hiccup must not flip a healthy connection to "down".
        model_count = 0

    per_provider = {
        provider.key: sum(
            1 for a in accounts_for_provider(accounts, provider) if a.healthy
        )
        for provider in SUBSCRIPTION_PROVIDERS
    }
    return {
        "reachable": True,
        "proxy_url": base,
        "error": None,
        "connected": any(a.healthy for a in accounts),
        "accounts": [a.to_public() for a in accounts],
        "model_count": model_count,
        "providers": [
            _provider_public(p, connected=per_provider[p.key])
            for p in SUBSCRIPTION_PROVIDERS
        ],
    }


def _provider_public(
    provider: SubscriptionProvider, *, connected: int
) -> dict[str, Any]:
    return {
        "key": provider.key,
        "label": provider.label,
        "vendor": provider.vendor,
        "login_flow": provider.login_flow,
        "channels": list(provider.channels),
        "client_protocol": provider.client_protocol,
        "has_usage_reader": provider.has_usage_reader,
        "inference_verified": provider.inference_verified,
        "notes": list(provider.notes),
        "connected_accounts": connected,
    }


# ---------------------------------------------------------------------------
# Login sessions
# ---------------------------------------------------------------------------


@dataclass
class LoginSession:
    """An SRW-owned login attempt bound to one user and one upstream state.

    The browser is handed ``login_id`` only. The upstream ``state`` never
    leaves the orchestrator: treating a browser-supplied provider/state pair as
    authority would let anyone with a stray callback URL drive someone else's
    connection.
    """

    login_id: str
    provider_key: str
    user_id: str | None
    state: str
    flow: str
    auth_url: str
    created_at: datetime
    expires_at: datetime
    user_code: str | None = None
    #: ``pending`` | ``verifying`` | ``connected`` | ``failed`` | ``cancelled``
    status: str = "pending"
    error: str | None = None
    #: (name, updated_at) of the provider's credentials when the flow started.
    baseline: frozenset[tuple[str, str | None]] = frozenset()
    #: When upstream first reported completion; starts the reconcile window.
    completed_at: datetime | None = None
    account_id: str | None = None
    callback_received: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "login_id": self.login_id,
            "provider": self.provider_key,
            "flow": self.flow,
            "status": self.status,
            "error": self.error,
            "auth_url": self.auth_url,
            "user_code": self.user_code,
            "expires_at": self.expires_at.isoformat(),
            "account_id": self.account_id,
            "accepts_callback_url": self.flow == LOGIN_FLOW_BROWSER,
        }


_login_sessions: dict[str, LoginSession] = {}
_login_lock = asyncio.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _purge_expired(now: datetime | None = None) -> None:
    now = now or _now()
    stale = [
        login_id
        for login_id, session in _login_sessions.items()
        if session.expires_at < now and session.status in {"pending", "verifying"}
    ]
    for login_id in stale:
        session = _login_sessions[login_id]
        session.status = "failed"
        session.error = "timeout"
    # Keep terminal sessions briefly so a late poll still sees the outcome.
    cutoff = (now - LOGIN_SESSION_TTL).timestamp()
    for login_id in [
        lid for lid, s in _login_sessions.items() if s.created_at.timestamp() < cutoff
    ]:
        _login_sessions.pop(login_id, None)


def _baseline_of(
    accounts: Iterable[SubscriptionAccount], provider: SubscriptionProvider
) -> frozenset[tuple[str, str | None]]:
    return frozenset(
        (a.name, a.updated_at) for a in accounts_for_provider(accounts, provider)
    )


async def start_login(provider_key: str, *, user_id: str | None) -> LoginSession:
    """Begin an authorization flow for ``provider_key``.

    Snapshots the provider's current credentials first: that snapshot is what
    later distinguishes "the browser came back" from "a credential was actually
    written", which is the difference between a truthful success and the
    permissive-poll failure mode the design calls out.
    """
    provider = get_provider(provider_key)
    if provider is None:
        raise SubscriptionProxyError(
            f"Unsupported subscription provider {provider_key!r}", status=400
        )

    try:
        baseline = _baseline_of(await list_accounts(use_cache=False), provider)
    except SubscriptionProxyError:
        raise

    response = await management_request(
        "GET",
        provider.login_path,
        params={"is_webui": "true"},
        timeout=20.0,
    )
    payload = _json_or_empty(response)
    if not isinstance(payload, Mapping):
        raise SubscriptionProxyError(
            "Subscription proxy returned an unusable login response"
        )

    state = _as_text(payload.get("state"))
    auth_url = _as_text(payload.get("url")) or _as_text(payload.get("auth_url"))
    if not state or not auth_url:
        raise SubscriptionProxyError(
            "Subscription proxy did not return an authorization URL and state"
        )

    upstream_flow = _as_text(payload.get("flow"))
    flow = LOGIN_FLOW_DEVICE if upstream_flow == "device" else provider.login_flow
    now = _now()
    expires_in = payload.get("expires_in")
    try:
        ttl = timedelta(seconds=int(expires_in)) if expires_in else LOGIN_SESSION_TTL
    except (TypeError, ValueError):
        ttl = LOGIN_SESSION_TTL
    ttl = min(ttl, LOGIN_SESSION_TTL)

    session = LoginSession(
        login_id=secrets.token_urlsafe(16),
        provider_key=provider.key,
        user_id=user_id,
        state=state,
        flow=flow,
        auth_url=auth_url,
        created_at=now,
        expires_at=now + ttl,
        user_code=_as_text(payload.get("user_code")),
        baseline=baseline,
    )
    async with _login_lock:
        _purge_expired(now)
        _login_sessions[session.login_id] = session
    logger.info(
        "subscription login started: provider=%s flow=%s login_id=%s",
        provider.key,
        flow,
        session.login_id,
    )
    return session


def get_login(login_id: str, *, user_id: str | None) -> LoginSession:
    """Fetch a login session, enforcing the initiating-user binding."""
    _purge_expired()
    session = _login_sessions.get(login_id)
    if session is None:
        raise SubscriptionProxyError("Unknown or expired login session", status=404)
    if (
        session.user_id is not None
        and user_id is not None
        and session.user_id != user_id
    ):
        # Another admin's in-flight connection. Report it as not-found rather
        # than as a permission error, which would confirm the id exists.
        raise SubscriptionProxyError("Unknown or expired login session", status=404)
    return session


async def _reconcile(session: LoginSession, provider: SubscriptionProvider) -> bool:
    """True once a credential for ``provider`` has demonstrably been written."""
    accounts = await list_accounts(use_cache=False)
    current = accounts_for_provider(accounts, provider)
    baseline_names = {name for name, _ in session.baseline}
    for account in current:
        key = (account.name, account.updated_at)
        if account.name not in baseline_names or key not in session.baseline:
            session.account_id = account.account_id
            return True
    return False


async def poll_login(session: LoginSession) -> LoginSession:
    """Advance a login session against upstream, then reconcile.

    Upstream's ``get-auth-status`` is deliberately not trusted on its own:

    * it answers ``{"status":"ok"}`` when asked with an *empty* state, so we
      always send ours and never treat a bare "ok" as provider-agnostic;
    * "ok" means the waiting goroutine reported completion, which is not the
      same as the credential being registered and usable.

    So an "ok" moves the session to ``verifying`` and only a credential that
    actually appears in the inventory moves it to ``connected``.
    """
    if session.status in {"connected", "failed", "cancelled"}:
        return session

    provider = get_provider(session.provider_key)
    if provider is None:  # pragma: no cover - registry is static
        session.status = "failed"
        session.error = "unsupported_provider"
        return session

    now = _now()
    if session.expires_at < now:
        session.status = "failed"
        session.error = "timeout"
        return session

    if session.status == "pending":
        try:
            payload = _json_or_empty(
                await management_request(
                    "GET",
                    "/v0/management/get-auth-status",
                    params={"state": session.state},
                )
            )
        except SubscriptionProxyUnreachable as exc:
            # Transport blip: stay pending rather than declaring a failure the
            # user cannot act on. The TTL still bounds the wait.
            logger.info("subscription login poll unreachable: %s", exc.message)
            return session
        status = ""
        if isinstance(payload, Mapping):
            status = (payload.get("status") or "").strip().lower()
        if status == "error":
            detail = (
                sanitize_upstream_detail(str(payload.get("error")))
                if isinstance(payload, Mapping)
                else ""
            )
            session.status = "failed"
            session.error = detail or "authorization_failed"
            return session
        if status == "ok":
            session.status = "verifying"
            session.completed_at = now
        else:
            return session

    if session.status == "verifying":
        try:
            if await _reconcile(session, provider):
                session.status = "connected"
                invalidate_account_cache()
                logger.info(
                    "subscription login connected: provider=%s login_id=%s",
                    provider.key,
                    session.login_id,
                )
                return session
        except SubscriptionProxyError as exc:
            logger.info("subscription login reconcile failed: %s", exc.message)
            return session
        started = session.completed_at or now
        if now - started > LOGIN_RECONCILE_GRACE:
            session.status = "failed"
            session.error = "credential_not_persisted"
    return session


async def cancel_login(session: LoginSession) -> LoginSession:
    """Cancel an in-flight authorization, upstream and locally."""
    if session.status in {"connected", "failed", "cancelled"}:
        return session
    try:
        await management_request(
            "DELETE", "/v0/management/oauth-session", params={"state": session.state}
        )
    except SubscriptionProxyError as exc:
        # The proxy may already have dropped the session; local cancellation
        # still stands so the UI stops waiting.
        logger.info("subscription login cancel upstream failed: %s", exc.message)
    session.status = "cancelled"
    session.error = None
    return session


def extract_callback_params(raw: str) -> tuple[str | None, str | None, str | None]:
    """Pull ``code`` / ``state`` / ``error`` out of a pasted callback URL.

    Parsing happens server-side and only the parameters are used. The URL the
    administrator pasted is never used as a *destination* — the orchestrator
    talks to its configured proxy and nothing else.
    """
    text = (raw or "").strip()
    if not text:
        return None, None, None
    parsed = urlparse(
        text if "://" in text else f"http://localhost/?{text.lstrip('?')}"
    )
    query = parse_qs(parsed.query)

    def one(key: str) -> str | None:
        values = query.get(key) or []
        return values[0].strip() if values and values[0].strip() else None

    return one("code"), one("state"), one("error") or one("error_description")


async def submit_callback(
    session: LoginSession,
    *,
    url: str | None = None,
    code: str | None = None,
    state: str | None = None,
) -> LoginSession:
    """Relay a browser OAuth callback for ``session`` to the proxy.

    Device flows never receive one, and a callback whose ``state`` does not
    match this session's upstream state is rejected outright.
    """
    provider = get_provider(session.provider_key)
    if provider is None or provider.callback_provider is None:
        raise SubscriptionProxyError(
            "This provider completes without a callback URL", status=400
        )
    if session.status in {"connected", "cancelled", "failed"}:
        raise SubscriptionProxyError(
            f"Login session is already {session.status}", status=409
        )

    parsed_code, parsed_state, parsed_error = extract_callback_params(url or "")
    code = code or parsed_code
    state = state or parsed_state
    if parsed_error and not code:
        session.status = "failed"
        session.error = sanitize_upstream_detail(parsed_error, limit=120)
        return session
    if not code:
        raise SubscriptionProxyError(
            "Could not read 'code' from the pasted URL. Copy the complete "
            "address from the browser's address bar.",
            status=422,
        )
    if state and state != session.state:
        raise SubscriptionProxyError(
            "That callback belongs to a different sign-in attempt.", status=409
        )

    await management_request(
        "POST",
        "/v0/management/oauth-callback",
        json={
            "provider": provider.callback_provider,
            "code": code,
            "state": session.state,
        },
        timeout=20.0,
    )
    # Accepting the callback only means the code reached the proxy. Completion
    # is still decided by poll_login → reconcile.
    session.callback_received = True
    invalidate_account_cache()
    return await poll_login(session)


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

#: The authoritative ChatGPT subscription rate-limit windows (5h + weekly) —
#: the same source the codex CLI's /status polls. Not exposed by the proxy.
CHATGPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


def decode_jwt_claims(token: str) -> dict[str, Any]:
    """Best-effort decode of a JWT payload (no signature check).

    We only read the non-secret account-scoping claim out of a token we already
    hold; nothing is trusted from it beyond routing the usage request.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def chatgpt_account_id(auth_file: Mapping[str, Any]) -> str | None:
    claims = decode_jwt_claims(auth_file.get("id_token", "") or "")
    auth = claims.get("https://api.openai.com/auth", {}) or {}
    return auth.get("chatgpt_account_id") or claims.get("chatgpt_account_id")


def usage_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, Mapping):
        return None
    return {
        "used_percent": window.get("used_percent"),
        "window_seconds": window.get("limit_window_seconds"),
        "reset_after_seconds": window.get("reset_after_seconds"),
        "reset_at": window.get("reset_at"),
    }


async def fetch_codex_usage(account: SubscriptionAccount) -> dict[str, Any] | None:
    """ChatGPT rate-limit windows for one **Codex** credential.

    Hard-scoped on purpose: the OAuth token is downloaded server-side and sent
    to a fixed ChatGPT destination, so the caller must have established that
    this credential is a Codex credential. Sending any other provider's token
    here would hand a third party's credential to OpenAI.
    """
    if account.channel != "codex":
        raise SubscriptionProxyError(
            "Usage for this provider is not available", status=400
        )
    try:
        token_file = _json_or_empty(
            await management_request(
                "GET",
                "/v0/management/auth-files/download",
                params={"name": account.name},
            )
        )
    except SubscriptionProxyError:
        return None
    if not isinstance(token_file, Mapping):
        return None
    access_token = token_file.get("access_token")
    if not access_token:
        return None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "srw-subscription-usage/1.0",
    }
    chatgpt_account = chatgpt_account_id(token_file)
    if chatgpt_account:
        headers["ChatGPT-Account-Id"] = chatgpt_account
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(CHATGPT_USAGE_URL, headers=headers)
        if response.status_code >= 400:
            logger.warning("chatgpt wham/usage returned HTTP %s", response.status_code)
            return None
        data = response.json()
    except Exception:
        logger.warning("chatgpt wham/usage fetch failed (non-fatal)", exc_info=True)
        return None
    if not isinstance(data, Mapping):
        return None

    rate_limit = data.get("rate_limit") or {}
    credits = data.get("credits") or {}
    return {
        "account": data.get("email"),
        "plan_type": data.get("plan_type"),
        "limit_reached": bool(rate_limit.get("limit_reached")),
        "primary": usage_window(rate_limit.get("primary_window")),
        "secondary": usage_window(rate_limit.get("secondary_window")),
        "per_model": [
            {
                "name": extra.get("limit_name"),
                "primary": usage_window(
                    (extra.get("rate_limit") or {}).get("primary_window")
                ),
                "secondary": usage_window(
                    (extra.get("rate_limit") or {}).get("secondary_window")
                ),
            }
            for extra in (data.get("additional_rate_limits") or [])
            if isinstance(extra, Mapping)
        ],
        "credits": {
            "has_credits": bool(credits.get("has_credits")),
            "unlimited": bool(credits.get("unlimited")),
            "balance": credits.get("balance"),
        }
        if credits
        else None,
    }


async def account_usage(account_id: str) -> dict[str, Any]:
    """Usage for one connected account, selected by provider.

    A provider without a verified reader reports ``available: false`` with a
    reason — never a zero that would read as "no quota used".
    """
    name = decode_account_id(account_id)
    accounts = await list_accounts(use_cache=False)
    account = next((a for a in accounts if a.name == name), None)
    if account is None:
        raise SubscriptionProxyError("Unknown subscription account", status=404)
    provider = get_provider(account.provider_key)
    if provider is None or not provider.has_usage_reader:
        return {
            "available": False,
            "reason": "unsupported_provider",
            "provider": account.provider_key,
            "account_id": account.account_id,
        }
    usage = await fetch_codex_usage(account)
    if usage is None:
        return {
            "available": False,
            "reason": "upstream_unavailable",
            "provider": account.provider_key,
            "account_id": account.account_id,
        }
    return {
        "available": True,
        "provider": account.provider_key,
        "account_id": account.account_id,
        **usage,
    }


async def first_codex_usage() -> dict[str, Any] | None:
    """Usage for the first healthy Codex account (legacy ``/api/codex/usage``).

    Returns ``None`` when no Codex credential is connected — importantly, it
    never falls back to "the first active account of any provider", which in a
    mixed pool would send someone else's token to ChatGPT.
    """
    try:
        accounts = await list_accounts(use_cache=False)
    except SubscriptionProxyError:
        return None
    account = next(
        (a for a in accounts if a.channel == "codex" and a.healthy),
        None,
    )
    if account is None:
        return None
    return await fetch_codex_usage(account)


# ---------------------------------------------------------------------------
# Model inventory + discovery enrichment
# ---------------------------------------------------------------------------


@dataclass
class AccountModels:
    """Per-credential model inventory read from the management API."""

    account: SubscriptionAccount
    models: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


async def account_models(account: SubscriptionAccount) -> AccountModels:
    try:
        payload = _json_or_empty(
            await management_request(
                "GET",
                "/v0/management/auth-files/models",
                params={"name": account.name},
            )
        )
    except SubscriptionProxyError as exc:
        return AccountModels(account=account, error=exc.message)
    raw = payload.get("models") if isinstance(payload, Mapping) else None
    models = [item for item in (raw or []) if isinstance(item, Mapping)]
    return AccountModels(account=account, models=[dict(m) for m in models])


async def account_model_map() -> tuple[dict[str, list[SubscriptionAccount]], list[str]]:
    """``{model_id: [accounts serving it]}`` plus the accounts we could not read.

    Bounded fan-out; a per-account failure degrades that account's
    contribution to "unknown" instead of failing the whole inventory. The
    second return value names the accounts whose inventory read failed so the
    caller can say the metadata is incomplete rather than pretend it is whole.
    """
    accounts = [a for a in await list_accounts(use_cache=False) if not a.disabled]
    semaphore = asyncio.Semaphore(_INVENTORY_CONCURRENCY)

    async def read(account: SubscriptionAccount) -> AccountModels:
        async with semaphore:
            return await account_models(account)

    results = await asyncio.gather(*(read(a) for a in accounts))
    mapping: dict[str, list[SubscriptionAccount]] = {}
    failed: list[str] = []
    for result in results:
        if result.error is not None:
            failed.append(result.account.account_id)
            continue
        for model in result.models:
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            mapping.setdefault(model_id, []).append(result.account)
    return mapping, failed


_definitions_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


async def channel_model_definitions(channel: str) -> dict[str, dict[str, Any]]:
    """Static model metadata the proxy publishes for one channel.

    Treated as *suggestions* (context window, display name, modalities) for
    models we already know are routable — never as extra models to import.
    """
    key = channel.strip().lower()
    cached = _definitions_cache.get(key)
    if cached and (time.monotonic() - cached[0]) < 300.0:
        return cached[1]
    try:
        payload = _json_or_empty(
            await management_request("GET", f"/v0/management/model-definitions/{key}")
        )
    except SubscriptionProxyError:
        return {}
    raw = payload.get("models") if isinstance(payload, Mapping) else None
    definitions = {
        item["id"]: dict(item)
        for item in (raw or [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    _definitions_cache[key] = (time.monotonic(), definitions)
    return definitions
