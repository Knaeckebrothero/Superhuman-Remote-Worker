"""Subscription-proxy management operations and the legacy Codex aliases.

Every upstream call goes through :mod:`orchestrator.services.subscriptions`,
which owns the management credential. Nothing here puts a key, an OAuth token
or an authorization code into a response body or a log line: upstream failure
detail reaches the caller only through ``SubscriptionProxyError.message``,
which the service layer has already trimmed and redacted
(``subscriptions.sanitize_upstream_detail``), and account payloads are always
the sanitized ``SubscriptionAccount.to_public()`` projection.

The Codex-scoped operations at the bottom back the legacy ``/api/codex/*``
routes. They filter the pool to Codex credentials on every path — a mixed pool
must never leak into a Codex-named answer, neither as an account list, a model
list, usage numbers, nor a disconnect.

Design: knowledge-base/knowledge/features/subscription_proxy.md §5, §8.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException

from orchestrator.schemas.subscriptions import CodexCallbackRequest
from orchestrator.services import subscription_discovery, subscriptions
from orchestrator.services.subscriptions import SubscriptionProxyError
from shared.subscription_routing import (
    CHANNEL_CODEX,
    SUBSCRIPTION_PROXY_TRANSPORT,
    is_subscription_endpoint,
)

#: SRW provider key of the ChatGPT/Codex connection — the one the legacy
#: ``/api/codex/*`` wrappers are scoped to.
CODEX_PROVIDER_KEY = "openai-codex"


def _endpoint_is_subscription_proxy(endpoint: Mapping[str, Any]) -> bool:
    """Whether an endpoint row is the shared subscription proxy.

    A row-shaped adapter over ``shared.subscription_routing`` — the single
    authority on proxy identity (``transport_kind`` first, the legacy labels
    and base URL as the upgrade path). Calling the shared predicate directly
    is what let the ``orchestrator.main`` → ``services.provider_catalog``
    private-name bridge go away in B02.
    """
    return is_subscription_endpoint(
        transport_kind=endpoint.get("transport_kind"),
        label=endpoint.get("label"),
        base_url=endpoint.get("base_url"),
    )


class SubscriptionEndpointStore(Protocol):
    def list_system_llm_endpoints(
        self,
    ) -> Awaitable[Sequence[Mapping[str, Any]]]: ...


class SubscriptionLogger(Protocol):
    def warning(self, msg: str, *args: Any, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class SubscriptionManagementDependencies:
    store: SubscriptionEndpointStore
    logger: SubscriptionLogger
    #: ``seed.llm_config.ensure_subscription_proxy_endpoint`` bound to the store.
    ensure_proxy_endpoint: Callable[[Any], Awaitable[Any]]


def subscription_http_error(exc: SubscriptionProxyError) -> HTTPException:
    """Map a service-layer failure onto an HTTP response.

    The message has already been trimmed and redacted by the service; this only
    picks the status so an unknown account is a 404 and an unreachable proxy is
    a 502.
    """
    return HTTPException(status_code=exc.status, detail=exc.message)


async def wire_subscription_endpoint(
    *, dependencies: SubscriptionManagementDependencies
) -> None:
    """Best-effort: make sure the transport row exists after a connection.

    A local login completes on the proxy's own callback port, so the
    orchestrator may never see a callback for it. Calling this whenever we
    observe a healthy account is what makes the subscription selectable in
    Admin → Models without an init re-run. Idempotent by transport marker.
    """
    try:
        await dependencies.ensure_proxy_endpoint(dependencies.store)
    except Exception:
        dependencies.logger.warning(
            "subscriptions: ensure_subscription_proxy_endpoint failed", exc_info=True
        )


# =============================================================================
# Availability probes
# =============================================================================


async def subscription_availability(
    *, dependencies: SubscriptionManagementDependencies
) -> dict[str, Any]:
    """Shared body for the availability probes.

    Reports whether the proxy is reachable, how many accounts are healthy per
    provider, and which endpoint row Discover/Add should target. Self-heals a
    live subscription with no transport row (a CLI login, or a row an admin
    deleted) so the next Admin → Models render lists the proxy.
    """
    endpoint_id: str | None = None
    for row in await dependencies.store.list_system_llm_endpoints():
        if _endpoint_is_subscription_proxy(row):
            endpoint_id = str(row["id"])
            break

    try:
        accounts = await subscriptions.list_accounts(use_cache=False)
    except SubscriptionProxyError as exc:
        return {
            "available": False,
            "reachable": False,
            "error": exc.message,
            "account_count": 0,
            "accounts": [],
            "models": [],
            "proxy_url": subscriptions.proxy_base_url(),
            "endpoint_id": endpoint_id,
        }

    healthy = [a for a in accounts if a.healthy]
    models: list[str] = []
    if healthy:
        models = sorted(await subscription_discovery.advertised_model_ids())
        if endpoint_id is None:
            await wire_subscription_endpoint(dependencies=dependencies)
            for row in await dependencies.store.list_system_llm_endpoints():
                if _endpoint_is_subscription_proxy(row):
                    endpoint_id = str(row["id"])
                    break

    return {
        "available": bool(healthy),
        "reachable": True,
        "error": None,
        "account_count": len(healthy),
        "accounts": [a.to_public() for a in healthy],
        "models": models,
        "proxy_url": subscriptions.proxy_base_url(),
        "endpoint_id": endpoint_id,
        "transport_kind": SUBSCRIPTION_PROXY_TRANSPORT,
    }


async def codex_availability(
    *, dependencies: SubscriptionManagementDependencies
) -> dict[str, Any]:
    """Legacy alias of the subscription availability probe.

    Kept for cockpit builds pinned to the Codex-only surface. The counts it
    reports now cover every connected subscription, so a client that reads it
    as "Codex accounts" over-counts in a mixed pool — that is precisely why the
    cockpit moved to the subscription route.
    """
    info = await subscription_availability(dependencies=dependencies)
    # The legacy shape carried no `accounts` array; drop it rather than hand a
    # Codex-named response a mixed-provider account list.
    return {
        "available": info["available"],
        "account_count": info["account_count"],
        "models": info["models"],
        "proxy_url": info["proxy_url"],
        "endpoint_id": info["endpoint_id"],
    }


# =============================================================================
# Proxy management surface
# =============================================================================


async def proxy_status(
    *, dependencies: SubscriptionManagementDependencies
) -> dict[str, Any]:
    """Proxy reachability, supported providers and connected accounts.

    Reachability is reported separately from authentication: a disabled or
    down proxy renders as "not enabled", not as "signed out". Provider support
    comes from the static registry — we never probe an auth-url handler, which
    would start a real authorization session as a side effect.
    """
    status = await subscriptions.proxy_status()
    if any(
        account.get("state") == "connected" for account in status.get("accounts", [])
    ):
        await wire_subscription_endpoint(dependencies=dependencies)
    return status


async def list_accounts() -> dict[str, Any]:
    """Sanitized inventory of connected subscription accounts."""
    try:
        accounts = await subscriptions.list_accounts()
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    return {"accounts": [account.to_public() for account in accounts]}


async def disconnect_account(*, account_id: str) -> dict[str, str]:
    """Disconnect one account (removes its credential from the proxy)."""
    try:
        await subscriptions.delete_account(account_id)
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    return {"status": "deleted"}


async def account_usage(*, account_id: str) -> dict[str, Any]:
    """Provider- and account-scoped usage.

    Only providers with a verified reader return numbers. Everything else
    reports ``available: false`` with a reason — never a zero, which would read
    as "no quota consumed". The ChatGPT reader is reachable only for a
    confirmed Codex credential.
    """
    try:
        return await subscriptions.account_usage(account_id)
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc


def _actor_id(user: Mapping[str, Any]) -> str | None:
    return str(user.get("id")) if user.get("id") else None


async def start_login(*, provider: str, user: Mapping[str, Any]) -> dict[str, Any]:
    """Start an authorization flow for one provider.

    Returns an SRW login id plus what the chosen flow needs: an authorization
    URL for a browser flow, or a verification URL + user code + expiry for a
    device flow. The upstream OAuth state stays server-side.
    """
    try:
        session = await subscriptions.start_login(provider, user_id=_actor_id(user))
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    return session.public()


async def poll_login(
    *,
    login_id: str,
    user: Mapping[str, Any],
    dependencies: SubscriptionManagementDependencies,
) -> dict[str, Any]:
    """Poll an authorization flow.

    ``connected`` is reported only after the credential has actually been
    observed in the proxy's inventory. Upstream answering "ok" moves the
    session to ``verifying``; a callback landing does not move it at all.
    """
    try:
        session = subscriptions.get_login(login_id, user_id=_actor_id(user))
        session = await subscriptions.poll_login(session)
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    if session.status == "connected":
        await wire_subscription_endpoint(dependencies=dependencies)
    return session.public()


async def submit_callback(
    *,
    login_id: str,
    url: str | None,
    code: str | None,
    state: str | None,
    user: Mapping[str, Any],
    dependencies: SubscriptionManagementDependencies,
) -> dict[str, Any]:
    """Relay a browser OAuth callback for a login session.

    The pasted URL is parsed server-side for its ``code``/``state`` only; the
    orchestrator posts to its configured proxy and never to a URL the browser
    supplied. A callback whose state belongs to another attempt is rejected.
    """
    try:
        session = subscriptions.get_login(login_id, user_id=_actor_id(user))
        session = await subscriptions.submit_callback(
            session, url=url, code=code, state=state
        )
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    if session.status == "connected":
        await wire_subscription_endpoint(dependencies=dependencies)
    return session.public()


async def cancel_login(*, login_id: str, user: Mapping[str, Any]) -> dict[str, Any]:
    """Cancel an in-flight authorization, upstream and locally."""
    try:
        session = subscriptions.get_login(login_id, user_id=_actor_id(user))
        session = await subscriptions.cancel_login(session)
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    return session.public()


# =============================================================================
# Legacy Codex-only operations
# =============================================================================
# Kept during rollout for clients pinned to the previous cockpit build. Each
# one filters the pool to Codex credentials, so an installation that has since
# connected Claude Code or Grok Build cannot see those accounts, their models
# or their usage through a Codex-named route.


async def codex_accounts() -> list[subscriptions.SubscriptionAccount]:
    accounts = await subscriptions.list_accounts()
    return [a for a in accounts if a.channel == CHANNEL_CODEX]


async def codex_model_ids(
    accounts: list[subscriptions.SubscriptionAccount],
) -> list[str]:
    """Models served by Codex credentials only.

    Attribution comes from the per-credential inventory. When that read fails
    we fall back to the advertised list *only* if Codex is the sole connected
    channel — otherwise another provider's models would surface under a
    Codex-named route.
    """
    if not accounts:
        return []
    names = {a.name for a in accounts}
    try:
        attribution, unreadable = await subscriptions.account_model_map()
    except SubscriptionProxyError:
        attribution, unreadable = {}, [a.account_id for a in accounts]
    if attribution:
        return sorted(
            model_id
            for model_id, model_accounts in attribution.items()
            if any(account.name in names for account in model_accounts)
        )
    if unreadable:
        try:
            all_accounts = await subscriptions.list_accounts()
        except SubscriptionProxyError:
            return []
        if any(a.channel != CHANNEL_CODEX for a in all_accounts):
            return []
        return sorted(await subscription_discovery.advertised_model_ids())
    return []


async def codex_status(
    *, dependencies: SubscriptionManagementDependencies
) -> dict[str, Any]:
    """Codex proxy health and authentication status (legacy)."""
    try:
        accounts = await codex_accounts()
    except SubscriptionProxyError:
        # Proxy unreachable — the deployment is disabled or down. `reachable:
        # False` lets the cockpit show an "enable it" disclaimer instead of a
        # Connect button that would 502.
        return {
            "connected": False,
            "reachable": False,
            "accounts": [],
            "model_count": 0,
        }

    model_count = 0
    if accounts:
        model_count = len(await codex_model_ids(accounts))
        await wire_subscription_endpoint(dependencies=dependencies)

    return {
        "connected": any(a.healthy for a in accounts),
        "reachable": True,
        "accounts": [
            {
                "name": a.name,
                "status": a.state,
                "status_message": a.state_detail,
            }
            for a in accounts
        ],
        "model_count": model_count,
    }


async def codex_models() -> dict[str, Any]:
    """List models available through Codex credentials (legacy)."""
    try:
        accounts = await codex_accounts()
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    return {"models": await codex_model_ids(accounts)}


async def codex_usage() -> dict[str, Any]:
    """Codex subscription usage/limits (legacy).

    Non-fatal: returns ``{"available": false}`` when the proxy is disabled/down,
    no Codex account is connected, or the ChatGPT backend call fails. Never
    falls back to another provider's credential.
    """
    usage = await subscriptions.first_codex_usage()
    if usage is None:
        return {"available": False}
    return {"available": True, **usage}


async def codex_login(*, user: Mapping[str, Any]) -> dict[str, Any]:
    """Initiate Codex OAuth login (legacy).

    Returns the auth URL plus the upstream ``state`` the legacy cockpit polls
    with. New clients use ``/api/subscriptions/logins``, which keeps the state
    server-side and binds the attempt to the initiating admin.
    """
    try:
        session = await subscriptions.start_login(
            CODEX_PROVIDER_KEY,
            user_id=_actor_id(user),
        )
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    return {"status": "ok", "auth_url": session.auth_url, "state": session.state}


async def codex_login_poll(*, state: str) -> dict[str, Any]:
    """Poll a Codex OAuth login by upstream state (legacy)."""
    if not state.strip():
        # Upstream answers a bare {"status":"ok"} for an empty state. Refuse
        # rather than hand the caller a success it did not earn.
        raise HTTPException(status_code=422, detail="state is required")
    try:
        response = await subscriptions.management_request(
            "GET", "/v0/management/get-auth-status", params={"state": state}
        )
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    try:
        return response.json()
    except ValueError:
        return {"status": "wait"}


async def codex_callback(
    *,
    body: CodexCallbackRequest,
    dependencies: SubscriptionManagementDependencies,
) -> dict[str, Any]:
    """Relay a Codex OAuth callback to the proxy (legacy).

    Accepts the full localhost callback URL (or explicit code+state), parses it
    server-side and relays through the proxy's shared callback endpoint, which
    validates the state against a pending Codex session.
    """
    code, state = body.code, body.state
    if body.url:
        parsed_code, parsed_state, _ = subscriptions.extract_callback_params(body.url)
        code = code or parsed_code
        state = state or parsed_state

    if not code or not state:
        raise HTTPException(
            status_code=422,
            detail="Could not extract 'code' and 'state' from the provided URL. "
            "Please paste the complete URL from your browser address bar.",
        )

    try:
        await subscriptions.management_request(
            "POST",
            "/v0/management/oauth-callback",
            json={"provider": CHANNEL_CODEX, "code": code, "state": state},
            timeout=20.0,
        )
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc

    subscriptions.invalidate_account_cache()
    await wire_subscription_endpoint(dependencies=dependencies)
    return {"status": "ok"}


async def codex_delete_credential(*, name: str) -> dict[str, str]:
    """Remove a Codex proxy credential file (legacy).

    Scoped: refuses to delete a credential belonging to any other provider, so
    a legacy client cannot disconnect a Claude Code or Grok Build account
    through a Codex-named route.
    """
    try:
        accounts = await subscriptions.list_accounts(use_cache=False)
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    account = next((a for a in accounts if a.name == name), None)
    if account is None:
        raise HTTPException(status_code=404, detail="Unknown subscription account")
    if account.channel != CHANNEL_CODEX:
        raise HTTPException(
            status_code=409,
            detail=(
                "That account is not a Codex subscription. Disconnect it from "
                "Settings → AI Subscriptions."
            ),
        )
    try:
        await subscriptions.delete_account(account.account_id)
    except SubscriptionProxyError as exc:
        raise subscription_http_error(exc) from exc
    return {"status": "deleted"}
