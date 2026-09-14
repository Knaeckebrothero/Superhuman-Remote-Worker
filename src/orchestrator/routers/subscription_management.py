"""HTTP adapters for the AI Subscriptions proxy management surface.

Settings → AI Subscriptions and Admin → Models both sit on this router. Every
route is admin-gated; the ``/api/codex/*`` block at the bottom keeps the
Codex-scoped compatibility wrappers for clients that have not moved yet.

Design: knowledge-base/knowledge/features/subscription_proxy.md §5, §8.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.schemas.subscriptions import (
    CodexCallbackRequest,
    SubscriptionCallbackSubmit,
    SubscriptionLoginStart,
)
from orchestrator.services import subscription_management

router = APIRouter()


@dataclass(frozen=True)
class SubscriptionManagementDependencies:
    operations: subscription_management.SubscriptionManagementDependencies
    require_admin: Callable[[Request], Awaitable[dict[str, Any]]]


def get_subscription_management_dependencies(
    request: Request,
) -> SubscriptionManagementDependencies:
    return request.app.state.subscription_management_dependencies_factory()


# =============================================================================
# Availability probes
# =============================================================================


@router.get("/api/admin/providers/subscriptions/availability")
async def admin_subscriptions_availability(
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Report whether the subscription proxy is usable right now (admin).

    Used by Admin → Models to decide whether to surface "Subscription proxy"
    as a model source. ``available`` is true only when the proxy is reachable
    AND at least one credential is healthy.
    """
    await dependencies.require_admin(request)
    return await subscription_management.subscription_availability(
        dependencies=dependencies.operations
    )


@router.get("/api/admin/providers/codex/availability")
async def admin_codex_availability(
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Legacy alias of the subscription availability probe (admin).

    Kept for cockpit builds pinned to the Codex-only surface. The counts it
    reports now cover every connected subscription, so a client that reads it
    as "Codex accounts" over-counts in a mixed pool — that is precisely why the
    cockpit moved to the route above.
    """
    await dependencies.require_admin(request)
    return await subscription_management.codex_availability(
        dependencies=dependencies.operations
    )


# =============================================================================
# AI Subscriptions — proxy management surface (Admin-only)
# =============================================================================


@router.get("/api/subscriptions/status")
async def subscriptions_status(
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Proxy reachability, supported providers and connected accounts.

    Reachability is reported separately from authentication: a disabled or
    down proxy renders as "not enabled", not as "signed out". Provider support
    comes from the static registry — we never probe an auth-url handler, which
    would start a real authorization session as a side effect.
    """
    await dependencies.require_admin(request)
    return await subscription_management.proxy_status(
        dependencies=dependencies.operations
    )


@router.get("/api/subscriptions/accounts")
async def subscriptions_accounts(
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Sanitized inventory of connected subscription accounts."""
    await dependencies.require_admin(request)
    return await subscription_management.list_accounts()


@router.delete("/api/subscriptions/accounts/{account_id}")
async def subscriptions_disconnect_account(
    account_id: str,
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, str]:
    """Disconnect one account (removes its credential from the proxy)."""
    await dependencies.require_admin(request)
    return await subscription_management.disconnect_account(account_id=account_id)


@router.get("/api/subscriptions/accounts/{account_id}/usage")
async def subscriptions_account_usage(
    account_id: str,
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Provider- and account-scoped usage.

    Only providers with a verified reader return numbers. Everything else
    reports ``available: false`` with a reason — never a zero, which would read
    as "no quota consumed". The ChatGPT reader is reachable only for a
    confirmed Codex credential.
    """
    await dependencies.require_admin(request)
    return await subscription_management.account_usage(account_id=account_id)


@router.post("/api/subscriptions/logins")
async def subscriptions_start_login(
    body: SubscriptionLoginStart,
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Start an authorization flow for one provider.

    Returns an SRW login id plus what the chosen flow needs: an authorization
    URL for a browser flow, or a verification URL + user code + expiry for a
    device flow. The upstream OAuth state stays server-side.
    """
    user = await dependencies.require_admin(request)
    return await subscription_management.start_login(provider=body.provider, user=user)


@router.get("/api/subscriptions/logins/{login_id}")
async def subscriptions_poll_login(
    login_id: str,
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Poll an authorization flow.

    ``connected`` is reported only after the credential has actually been
    observed in the proxy's inventory. Upstream answering "ok" moves the
    session to ``verifying``; a callback landing does not move it at all.
    """
    user = await dependencies.require_admin(request)
    return await subscription_management.poll_login(
        login_id=login_id, user=user, dependencies=dependencies.operations
    )


@router.post("/api/subscriptions/logins/{login_id}/callback")
async def subscriptions_submit_callback(
    login_id: str,
    body: SubscriptionCallbackSubmit,
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Relay a browser OAuth callback for a login session.

    The pasted URL is parsed server-side for its ``code``/``state`` only; the
    orchestrator posts to its configured proxy and never to a URL the browser
    supplied. A callback whose state belongs to another attempt is rejected.
    """
    user = await dependencies.require_admin(request)
    return await subscription_management.submit_callback(
        login_id=login_id,
        url=body.url,
        code=body.code,
        state=body.state,
        user=user,
        dependencies=dependencies.operations,
    )


@router.delete("/api/subscriptions/logins/{login_id}")
async def subscriptions_cancel_login(
    login_id: str,
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Cancel an in-flight authorization, upstream and locally."""
    user = await dependencies.require_admin(request)
    return await subscription_management.cancel_login(login_id=login_id, user=user)


# =============================================================================
# Legacy Codex-only wrappers
# =============================================================================
# Kept during rollout for clients pinned to the previous cockpit build. Each
# one filters the pool to Codex credentials, so an installation that has since
# connected Claude Code or Grok Build cannot see those accounts, their models
# or their usage through a Codex-named route.


@router.get("/api/codex/status")
async def codex_status(
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Codex proxy health and authentication status (admin-only, legacy)."""
    await dependencies.require_admin(request)
    return await subscription_management.codex_status(
        dependencies=dependencies.operations
    )


@router.get("/api/codex/models")
async def codex_models(
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """List models available through Codex credentials (admin-only, legacy)."""
    await dependencies.require_admin(request)
    return await subscription_management.codex_models()


@router.get("/api/codex/usage")
async def codex_usage(
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Codex subscription usage/limits (admin-only, legacy).

    Non-fatal: returns ``{"available": false}`` when the proxy is disabled/down,
    no Codex account is connected, or the ChatGPT backend call fails. Never
    falls back to another provider's credential.
    """
    await dependencies.require_admin(request)
    return await subscription_management.codex_usage()


@router.post("/api/codex/login")
async def codex_login(
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Initiate Codex OAuth login (admin-only, legacy).

    Returns the auth URL plus the upstream ``state`` the legacy cockpit polls
    with. New clients use ``/api/subscriptions/logins``, which keeps the state
    server-side and binds the attempt to the initiating admin.
    """
    user = await dependencies.require_admin(request)
    return await subscription_management.codex_login(user=user)


@router.get("/api/codex/login/poll")
async def codex_login_poll(
    request: Request,
    state: str,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Poll a Codex OAuth login by upstream state (admin-only, legacy)."""
    await dependencies.require_admin(request)
    return await subscription_management.codex_login_poll(state=state)


@router.post("/api/codex/callback")
async def codex_callback(
    body: CodexCallbackRequest,
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, Any]:
    """Relay a Codex OAuth callback to the proxy (admin-only, legacy).

    Accepts the full localhost callback URL (or explicit code+state), parses it
    server-side and relays through the proxy's shared callback endpoint, which
    validates the state against a pending Codex session.
    """
    await dependencies.require_admin(request)
    return await subscription_management.codex_callback(
        body=body, dependencies=dependencies.operations
    )


@router.delete("/api/codex/credentials/{name}")
async def codex_delete_credential(
    name: str,
    request: Request,
    *,
    dependencies: SubscriptionManagementDependencies = Depends(
        get_subscription_management_dependencies
    ),
) -> dict[str, str]:
    """Remove a Codex proxy credential file (admin-only, legacy).

    Scoped: refuses to delete a credential belonging to any other provider, so
    a legacy client cannot disconnect a Claude Code or Grok Build account
    through a Codex-named route.
    """
    await dependencies.require_admin(request)
    return await subscription_management.codex_delete_credential(name=name)
