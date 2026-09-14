"""Wire contracts for the AI Subscriptions surface and its Codex legacy aliases.

Characterized against the pre-extraction ``main.py`` handlers and ported onto
``orchestrator.routers.subscription_management`` unchanged.

Two properties carry the security weight here:

* **Redaction survives the move.** The service layer trims and redacts upstream
  detail before it becomes a ``SubscriptionProxyError``; the adapter only picks
  the status. Nothing in a response body or a probe payload may carry an OAuth
  token, an authorization code or the management key.
* **A Codex-named route never answers about another provider.** With a mixed
  pool connected, the legacy aliases must not list, count, enumerate models
  for, or disconnect a Claude Code / Grok Build account.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from orchestrator.services import subscriptions
from orchestrator.services.subscriptions import (
    SubscriptionAccount,
    SubscriptionProxyError,
)
from shared.subscription_routing import CHANNEL_CODEX, SUBSCRIPTION_PROXY_TRANSPORT
from tests._mounted_router import mount_router


ADMIN = {"id": "00000000-0000-0000-0000-0000000000ad", "email": "admin@test"}
ENDPOINT_ID = "00000000-0000-0000-0000-0000000000e1"
TOKEN = "ya29.super-secret-oauth-token"


def _account(name="chatgpt-1", channel=CHANNEL_CODEX, state="connected"):
    return SubscriptionAccount(
        account_id=f"id-{name}",
        name=name,
        channel=channel,
        provider_key="openai-codex" if channel == CHANNEL_CODEX else "anthropic-claude",
        label=name,
        email=None,
        account_type=None,
        state=state,
        state_detail=None,
        disabled=False,
        unavailable=False,
        next_retry_after=None,
        updated_at=None,
        last_refresh=None,
        auth_index=None,
    )


def _wire(monkeypatch, *, endpoints=None, ensure=None, store=None):
    """Mount the router with a scriptable store and a recording ensure hook."""
    from orchestrator.routers.subscription_management import (
        SubscriptionManagementDependencies as RouteDeps,
    )
    from orchestrator.routers.subscription_management import router
    from orchestrator.services.subscription_management import (
        SubscriptionManagementDependencies as OpDeps,
    )

    db = store or SimpleNamespace(
        list_system_llm_endpoints=AsyncMock(return_value=list(endpoints or []))
    )
    logger = MagicMock()
    ensure_hook = ensure or AsyncMock()

    async def admin(_request):
        return ADMIN

    deps = RouteDeps(
        operations=OpDeps(store=db, logger=logger, ensure_proxy_endpoint=ensure_hook),
        require_admin=admin,
    )
    app = mount_router(
        router,
        factories={"subscription_management_dependencies_factory": lambda: deps},
    )
    return SimpleNamespace(
        client=TestClient(app), store=db, logger=logger, ensure=ensure_hook
    )


# =============================================================================
# Availability probes
# =============================================================================


def test_availability_reports_unreachable_when_the_proxy_is_down(monkeypatch):
    monkeypatch.setattr(
        subscriptions,
        "list_accounts",
        AsyncMock(side_effect=SubscriptionProxyError("proxy disabled", status=502)),
    )
    monkeypatch.setattr(subscriptions, "proxy_base_url", lambda: "http://proxy.test")
    wire = _wire(monkeypatch)

    resp = wire.client.get("/api/admin/providers/subscriptions/availability")

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reachable"] is False
    assert body["error"] == "proxy disabled"
    assert body["account_count"] == 0
    assert body["accounts"] == []
    assert body["models"] == []
    # The unreachable shape carries no transport_kind — characterized, not chosen.
    assert "transport_kind" not in body


def test_availability_reports_healthy_accounts_and_transport(monkeypatch):
    monkeypatch.setattr(
        subscriptions, "list_accounts", AsyncMock(return_value=[_account()])
    )
    monkeypatch.setattr(subscriptions, "proxy_base_url", lambda: "http://proxy.test")
    from orchestrator.services import subscription_discovery

    monkeypatch.setattr(
        subscription_discovery,
        "advertised_model_ids",
        AsyncMock(return_value={"m2", "m1"}),
    )
    wire = _wire(
        monkeypatch,
        endpoints=[{"id": ENDPOINT_ID, "transport_kind": SUBSCRIPTION_PROXY_TRANSPORT}],
    )

    body = wire.client.get("/api/admin/providers/subscriptions/availability").json()

    assert body["available"] is True
    assert body["reachable"] is True
    assert body["account_count"] == 1
    assert body["models"] == ["m1", "m2"]
    assert body["endpoint_id"] == ENDPOINT_ID
    assert body["transport_kind"] == SUBSCRIPTION_PROXY_TRANSPORT
    # A row already exists: no self-heal write.
    wire.ensure.assert_not_awaited()


def test_availability_self_heals_a_missing_transport_row(monkeypatch):
    monkeypatch.setattr(
        subscriptions, "list_accounts", AsyncMock(return_value=[_account()])
    )
    monkeypatch.setattr(subscriptions, "proxy_base_url", lambda: "http://proxy.test")
    from orchestrator.services import subscription_discovery

    monkeypatch.setattr(
        subscription_discovery, "advertised_model_ids", AsyncMock(return_value=set())
    )
    rows = [[], [{"id": ENDPOINT_ID, "transport_kind": SUBSCRIPTION_PROXY_TRANSPORT}]]
    store = SimpleNamespace(
        list_system_llm_endpoints=AsyncMock(side_effect=lambda: rows.pop(0))
    )
    wire = _wire(monkeypatch, store=store)

    body = wire.client.get("/api/admin/providers/subscriptions/availability").json()

    wire.ensure.assert_awaited_once()
    assert body["endpoint_id"] == ENDPOINT_ID


def test_self_heal_failure_is_logged_not_raised(monkeypatch):
    monkeypatch.setattr(
        subscriptions, "list_accounts", AsyncMock(return_value=[_account()])
    )
    monkeypatch.setattr(subscriptions, "proxy_base_url", lambda: "http://proxy.test")
    from orchestrator.services import subscription_discovery

    monkeypatch.setattr(
        subscription_discovery, "advertised_model_ids", AsyncMock(return_value=set())
    )
    store = SimpleNamespace(list_system_llm_endpoints=AsyncMock(return_value=[]))
    wire = _wire(
        monkeypatch,
        store=store,
        ensure=AsyncMock(side_effect=RuntimeError("no db")),
    )

    body = wire.client.get("/api/admin/providers/subscriptions/availability").json()

    assert body["available"] is True
    assert body["endpoint_id"] is None
    wire.logger.warning.assert_called_once()


def test_codex_availability_alias_drops_the_mixed_account_list(monkeypatch):
    """A Codex-named response must not hand back a mixed-provider account list."""
    monkeypatch.setattr(
        subscriptions,
        "list_accounts",
        AsyncMock(return_value=[_account(), _account("claude-1", channel="claude")]),
    )
    monkeypatch.setattr(subscriptions, "proxy_base_url", lambda: "http://proxy.test")
    from orchestrator.services import subscription_discovery

    monkeypatch.setattr(
        subscription_discovery, "advertised_model_ids", AsyncMock(return_value={"m1"})
    )
    wire = _wire(
        monkeypatch,
        endpoints=[{"id": ENDPOINT_ID, "transport_kind": SUBSCRIPTION_PROXY_TRANSPORT}],
    )

    body = wire.client.get("/api/admin/providers/codex/availability").json()

    assert set(body) == {
        "available",
        "account_count",
        "models",
        "proxy_url",
        "endpoint_id",
    }
    assert "accounts" not in body
    assert (
        "claude-1"
        not in wire.client.get("/api/admin/providers/codex/availability").text
    )


# =============================================================================
# Proxy management surface
# =============================================================================


def test_accounts_returns_only_the_sanitized_projection(monkeypatch):
    monkeypatch.setattr(
        subscriptions, "list_accounts", AsyncMock(return_value=[_account()])
    )
    wire = _wire(monkeypatch)

    body = wire.client.get("/api/subscriptions/accounts").json()

    assert body["accounts"] == [_account().to_public()]
    assert "auth_index" not in body["accounts"][0]
    assert "disabled" not in body["accounts"][0]


def test_accounts_maps_a_proxy_error_onto_its_status_and_redacted_message(monkeypatch):
    monkeypatch.setattr(
        subscriptions,
        "list_accounts",
        AsyncMock(side_effect=SubscriptionProxyError("upstream said no", status=502)),
    )
    wire = _wire(monkeypatch)

    resp = wire.client.get("/api/subscriptions/accounts")

    assert resp.status_code == 502
    assert resp.json()["detail"] == "upstream said no"


def test_unknown_account_is_a_404_not_a_502(monkeypatch):
    monkeypatch.setattr(
        subscriptions,
        "delete_account",
        AsyncMock(
            side_effect=SubscriptionProxyError(
                "Unknown subscription account id", status=404
            )
        ),
    )
    wire = _wire(monkeypatch)

    resp = wire.client.delete("/api/subscriptions/accounts/id-nope")
    assert resp.status_code == 404


def test_status_wires_the_endpoint_only_when_something_is_connected(monkeypatch):
    monkeypatch.setattr(
        subscriptions,
        "proxy_status",
        AsyncMock(return_value={"accounts": [{"state": "signed_out"}]}),
    )
    wire = _wire(monkeypatch)
    wire.client.get("/api/subscriptions/status")
    wire.ensure.assert_not_awaited()

    monkeypatch.setattr(
        subscriptions,
        "proxy_status",
        AsyncMock(return_value={"accounts": [{"state": "connected"}]}),
    )
    wire2 = _wire(monkeypatch)
    wire2.client.get("/api/subscriptions/status")
    wire2.ensure.assert_awaited_once()


def test_login_start_binds_the_attempt_to_the_admin_and_hides_upstream_state(
    monkeypatch,
):
    session = SimpleNamespace(
        public=lambda: {"login_id": "srw-1", "status": "pending"},
        status="pending",
    )
    start = AsyncMock(return_value=session)
    monkeypatch.setattr(subscriptions, "start_login", start)
    wire = _wire(monkeypatch)

    body = wire.client.post(
        "/api/subscriptions/logins", json={"provider": "openai-codex"}
    ).json()

    assert body == {"login_id": "srw-1", "status": "pending"}
    assert start.await_args.kwargs["user_id"] == ADMIN["id"]


def test_callback_relay_never_echoes_the_authorization_code(monkeypatch):
    session = SimpleNamespace(
        public=lambda: {"login_id": "srw-1", "status": "connected"},
        status="connected",
    )
    monkeypatch.setattr(subscriptions, "get_login", MagicMock(return_value=session))
    monkeypatch.setattr(
        subscriptions, "submit_callback", AsyncMock(return_value=session)
    )
    wire = _wire(monkeypatch)

    resp = wire.client.post(
        "/api/subscriptions/logins/srw-1/callback",
        json={"url": f"http://localhost:1455/cb?code={TOKEN}&state=st-1"},
    )

    assert resp.status_code == 200
    assert TOKEN not in resp.text
    wire.ensure.assert_awaited_once()


# =============================================================================
# Legacy Codex aliases — provider scoping
# =============================================================================


def test_codex_status_reports_unreachable_rather_than_failing(monkeypatch):
    monkeypatch.setattr(
        subscriptions,
        "list_accounts",
        AsyncMock(side_effect=SubscriptionProxyError("down", status=502)),
    )
    wire = _wire(monkeypatch)

    body = wire.client.get("/api/codex/status").json()

    assert body == {
        "connected": False,
        "reachable": False,
        "accounts": [],
        "model_count": 0,
    }


def test_codex_status_lists_only_codex_accounts(monkeypatch):
    monkeypatch.setattr(
        subscriptions,
        "list_accounts",
        AsyncMock(return_value=[_account(), _account("claude-1", channel="claude")]),
    )
    monkeypatch.setattr(
        subscriptions,
        "account_model_map",
        AsyncMock(return_value=({"gpt-x": [_account()]}, [])),
    )
    wire = _wire(monkeypatch)

    resp = wire.client.get("/api/codex/status")

    assert resp.status_code == 200
    assert [a["name"] for a in resp.json()["accounts"]] == ["chatgpt-1"]
    assert "claude-1" not in resp.text


def test_codex_models_never_includes_another_providers_models(monkeypatch):
    codex, claude = _account(), _account("claude-1", channel="claude")
    monkeypatch.setattr(
        subscriptions, "list_accounts", AsyncMock(return_value=[codex, claude])
    )
    monkeypatch.setattr(
        subscriptions,
        "account_model_map",
        AsyncMock(return_value=({"gpt-x": [codex], "claude-x": [claude]}, [])),
    )
    wire = _wire(monkeypatch)

    assert wire.client.get("/api/codex/models").json() == {"models": ["gpt-x"]}


def test_codex_models_refuses_the_advertised_fallback_in_a_mixed_pool(monkeypatch):
    """Attribution unreadable + another channel connected → answer nothing."""
    codex, claude = _account(), _account("claude-1", channel="claude")
    monkeypatch.setattr(
        subscriptions, "list_accounts", AsyncMock(return_value=[codex, claude])
    )
    monkeypatch.setattr(
        subscriptions,
        "account_model_map",
        AsyncMock(return_value=({}, ["id-chatgpt-1"])),
    )
    from orchestrator.services import subscription_discovery

    advertised = AsyncMock(return_value={"gpt-x", "claude-x"})
    monkeypatch.setattr(subscription_discovery, "advertised_model_ids", advertised)
    wire = _wire(monkeypatch)

    assert wire.client.get("/api/codex/models").json() == {"models": []}
    advertised.assert_not_awaited()


def test_codex_models_uses_the_advertised_fallback_when_codex_is_alone(monkeypatch):
    codex = _account()
    monkeypatch.setattr(subscriptions, "list_accounts", AsyncMock(return_value=[codex]))
    monkeypatch.setattr(
        subscriptions,
        "account_model_map",
        AsyncMock(return_value=({}, ["id-chatgpt-1"])),
    )
    from orchestrator.services import subscription_discovery

    monkeypatch.setattr(
        subscription_discovery,
        "advertised_model_ids",
        AsyncMock(return_value={"gpt-x"}),
    )
    wire = _wire(monkeypatch)

    assert wire.client.get("/api/codex/models").json() == {"models": ["gpt-x"]}


def test_codex_delete_refuses_a_non_codex_account_with_409(monkeypatch):
    claude = _account("claude-1", channel="claude")
    monkeypatch.setattr(
        subscriptions, "list_accounts", AsyncMock(return_value=[claude])
    )
    delete = AsyncMock()
    monkeypatch.setattr(subscriptions, "delete_account", delete)
    wire = _wire(monkeypatch)

    resp = wire.client.delete("/api/codex/credentials/claude-1")

    assert resp.status_code == 409
    assert "not a Codex subscription" in resp.json()["detail"]
    delete.assert_not_awaited()


def test_codex_delete_404s_an_unknown_name(monkeypatch):
    monkeypatch.setattr(subscriptions, "list_accounts", AsyncMock(return_value=[]))
    wire = _wire(monkeypatch)
    resp = wire.client.delete("/api/codex/credentials/ghost")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Unknown subscription account"


def test_codex_delete_removes_a_codex_account(monkeypatch):
    monkeypatch.setattr(
        subscriptions, "list_accounts", AsyncMock(return_value=[_account()])
    )
    delete = AsyncMock()
    monkeypatch.setattr(subscriptions, "delete_account", delete)
    wire = _wire(monkeypatch)

    resp = wire.client.delete("/api/codex/credentials/chatgpt-1")

    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted"}
    delete.assert_awaited_once_with("id-chatgpt-1")


def test_codex_usage_is_non_fatal_when_no_codex_account_is_connected(monkeypatch):
    monkeypatch.setattr(
        subscriptions, "first_codex_usage", AsyncMock(return_value=None)
    )
    wire = _wire(monkeypatch)
    assert wire.client.get("/api/codex/usage").json() == {"available": False}


def test_codex_login_poll_refuses_an_empty_state(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(subscriptions, "management_request", request)
    wire = _wire(monkeypatch)

    resp = wire.client.get("/api/codex/login/poll", params={"state": "   "})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "state is required"
    request.assert_not_awaited()


def test_codex_callback_requires_a_parseable_code_and_state(monkeypatch):
    request = AsyncMock()
    monkeypatch.setattr(subscriptions, "management_request", request)
    wire = _wire(monkeypatch)

    resp = wire.client.post("/api/codex/callback", json={"url": "http://localhost/cb"})

    assert resp.status_code == 422
    assert "Could not extract" in resp.json()["detail"]
    request.assert_not_awaited()


def test_codex_callback_relays_and_invalidates_without_echoing_the_code(monkeypatch):
    request = AsyncMock(return_value=SimpleNamespace(json=lambda: {"status": "ok"}))
    invalidate = MagicMock()
    monkeypatch.setattr(subscriptions, "management_request", request)
    monkeypatch.setattr(subscriptions, "invalidate_account_cache", invalidate)
    wire = _wire(monkeypatch)

    resp = wire.client.post(
        "/api/codex/callback",
        json={"url": f"http://localhost:1455/cb?code={TOKEN}&state=st-1"},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert TOKEN not in resp.text
    invalidate.assert_called_once()
    wire.ensure.assert_awaited_once()
    assert request.await_args.kwargs["json"]["provider"] == CHANNEL_CODEX


# =============================================================================
# Per-application dependency isolation
# =============================================================================


def test_each_application_resolves_its_own_collaborators(monkeypatch):
    monkeypatch.setattr(
        subscriptions,
        "list_accounts",
        AsyncMock(side_effect=SubscriptionProxyError("down", status=502)),
    )
    monkeypatch.setattr(subscriptions, "proxy_base_url", lambda: "http://proxy.test")
    first, second = _wire(monkeypatch), _wire(monkeypatch)

    first.client.get("/api/admin/providers/subscriptions/availability")

    first.store.list_system_llm_endpoints.assert_awaited_once()
    second.store.list_system_llm_endpoints.assert_not_awaited()


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/subscriptions/status"),
        ("get", "/api/codex/status"),
        ("get", "/api/admin/providers/subscriptions/availability"),
    ],
)
def test_every_route_runs_the_admin_gate(monkeypatch, method, path):
    from orchestrator.routers.subscription_management import (
        SubscriptionManagementDependencies as RouteDeps,
    )
    from orchestrator.routers.subscription_management import router
    from orchestrator.services.subscription_management import (
        SubscriptionManagementDependencies as OpDeps,
    )
    from fastapi import HTTPException

    async def deny(_request):
        raise HTTPException(status_code=403, detail="Admin access required")

    deps = RouteDeps(
        operations=OpDeps(
            store=SimpleNamespace(list_system_llm_endpoints=AsyncMock(return_value=[])),
            logger=MagicMock(),
            ensure_proxy_endpoint=AsyncMock(),
        ),
        require_admin=deny,
    )
    app = mount_router(
        router,
        factories={"subscription_management_dependencies_factory": lambda: deps},
    )
    resp = getattr(TestClient(app), method)(path)
    assert resp.status_code == 403
