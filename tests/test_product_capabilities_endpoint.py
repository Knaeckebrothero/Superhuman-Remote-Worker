"""HTTP contract tests for the rollout-gated M2b capability endpoint."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from services.product_capabilities import (
    MAX_EXPLICIT_CAPABILITY_IDS,
    MAX_RESPONSE_BYTES,
    ProductCapabilityService,
)
from src.core.product_capabilities import (
    AgentAction,
    Completeness,
    EvaluationErrorCode,
    ProductCapabilitiesResponse,
    ScopeKind,
    SessionState,
    UserState,
)

_USER_ID = "11111111-1111-1111-1111-111111111111"
_OTHER_USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_THREAD_ID = "22222222-2222-2222-2222-222222222222"
_PROJECT_ID = "33333333-3333-3333-3333-333333333333"
_ROOT = Path(__file__).resolve().parents[1]


def _user(
    *,
    user_id: str = _USER_ID,
    is_admin: bool = False,
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": user_id,
        "is_admin": is_admin,
        "real_is_admin": is_admin,
        "is_approved": True,
        "auth_method": "cookie",
        "scopes": scopes or [],
    }


def _thread(*, owner_id: str = _USER_ID) -> dict[str, Any]:
    return {
        "id": _THREAD_ID,
        "user_id": owner_id,
        "project_id": _PROJECT_ID,
        "metadata": {
            "datasource_ids": ["private-datasource-id"],
            "workspace_backend": "virtual",
        },
    }


@pytest.fixture
def endpoint(monkeypatch):
    from orchestrator.routers import product_capabilities as routes

    state: dict[str, Any] = {
        "user": _user(),
        "thread": _thread(),
        "gate": True,
        "approved_error": None,
        "owner_error": None,
    }
    calls: dict[str, list[Any]] = {
        "approved": [],
        "owner": [],
        "grants": [],
    }
    database = object()

    async def require_approved(request, db):
        calls["approved"].append((request.url.path, db))
        error = state["approved_error"]
        if error is not None:
            raise error
        return state["user"]

    async def require_owner(request, db, thread_id):
        calls["owner"].append((request.url.path, db, thread_id))
        error = state["owner_error"]
        if error is not None:
            raise error
        return state["user"], state["thread"]

    async def grants(_db, **kwargs):
        calls["grants"].append(kwargs)
        return {
            "browser": True,
            "datasource_tools": True,
            "delegation": True,
            "email_autonomous_send": False,
            "permission_mode": "auto_accept",
        }

    monkeypatch.setattr(routes, "_get_db", lambda: database)
    monkeypatch.setattr(routes, "require_approved_user", require_approved)
    monkeypatch.setattr(routes, "require_thread_owner", require_owner)
    monkeypatch.setattr(
        routes,
        "product_capabilities_endpoint_enabled",
        lambda: bool(state["gate"]),
    )
    monkeypatch.setattr(
        routes,
        "_get_service",
        lambda db: ProductCapabilityService(
            db,
            grants_resolver=grants,
            environment={
                "CANVAS_SHARED_BROWSER_ENABLED": "false",
                "PROTECTED_CLOUD_MODE_ENABLED": "false",
            },
        ),
    )
    audit = AsyncMock()
    monkeypatch.setattr(routes, "log_security_event", audit)

    app = FastAPI()
    app.include_router(routes.router)
    client = TestClient(app, raise_server_exceptions=False)
    return client, state, calls, audit, routes


def test_user_scoped_email_response_is_valid_private_and_no_store(endpoint):
    client, _state, calls, _audit, routes = endpoint

    response = client.get(
        "/api/users/me/product-capabilities",
        params={"capability_id": "datasources.email"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers[routes.CAPABILITY_PLANE_HEADER] == "enabled"
    assert "etag" not in response.headers
    assert int(response.headers["content-length"]) <= MAX_RESPONSE_BYTES

    parsed = ProductCapabilitiesResponse.model_validate(response.json())
    assert parsed.scope.kind is ScopeKind.USER
    assert parsed.completeness is Completeness.COMPLETE
    assert [item.id for item in parsed.capabilities] == ["datasources.email"]
    capability = parsed.capabilities[0]
    assert capability.user.state is UserState.ALLOWED
    assert capability.session.state is SessionState.NOT_APPLICABLE
    assert capability.agent_action is AgentAction.UNKNOWN
    assert calls["approved"]
    assert not calls["owner"]
    assert calls["grants"] == [{"user_id": _USER_ID, "project_ids": []}]

    serialized = response.text
    for private_value in (
        _USER_ID,
        _PROJECT_ID,
        "private-datasource-id",
    ):
        assert private_value not in serialized


def test_thread_scope_delegates_to_owner_gate_and_primary_project(endpoint):
    client, _state, calls, _audit, _routes = endpoint

    response = client.get(
        "/api/users/me/product-capabilities",
        params={
            "thread_id": _THREAD_ID,
            "capability_id": "datasources.email",
        },
    )

    assert response.status_code == 200
    parsed = ProductCapabilitiesResponse.model_validate(response.json())
    assert parsed.scope.kind is ScopeKind.THREAD
    assert str(parsed.scope.thread_id) == _THREAD_ID
    assert parsed.capabilities[0].session.state is SessionState.UNKNOWN
    assert parsed.capabilities[0].agent_action is AgentAction.UNKNOWN
    assert calls["owner"] == [
        (
            "/api/users/me/product-capabilities",
            calls["owner"][0][1],
            _THREAD_ID,
        )
    ]
    assert not calls["approved"]
    assert calls["grants"] == [
        {
            "user_id": _USER_ID,
            "project_ids": [_PROJECT_ID],
        }
    ]


@pytest.mark.parametrize(
    ("status_code", "detail"),
    [
        (401, "Not authenticated"),
        (403, "Account pending approval"),
    ],
)
def test_unauthenticated_and_unapproved_callers_are_rejected(
    endpoint,
    status_code: int,
    detail: str,
):
    client, state, _calls, _audit, _routes = endpoint
    state["approved_error"] = HTTPException(
        status_code=status_code,
        detail=detail,
    )

    response = client.get("/api/users/me/product-capabilities")

    assert response.status_code == status_code
    assert response.json()["detail"] == detail


def test_non_owner_thread_is_rejected_before_resolution(endpoint):
    client, state, calls, _audit, _routes = endpoint
    state["owner_error"] = HTTPException(
        status_code=403,
        detail="Not your thread",
    )

    response = client.get(
        "/api/users/me/product-capabilities",
        params={"thread_id": _THREAD_ID},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Not your thread"
    assert not calls["grants"]


def test_admin_thread_request_uses_admitted_thread_without_grant_lookup(endpoint):
    client, state, calls, _audit, _routes = endpoint
    state["user"] = _user(is_admin=True)

    response = client.get(
        "/api/users/me/product-capabilities",
        params={
            "thread_id": _THREAD_ID,
            "capability_id": "datasources.email",
        },
    )

    assert response.status_code == 200
    parsed = ProductCapabilitiesResponse.model_validate(response.json())
    assert parsed.capabilities[0].user.reason_code.value == "admin_allowed"
    assert not calls["grants"]


def test_project_scoped_mcp_token_cannot_widen_to_user_scope(endpoint):
    client, state, calls, audit, _routes = endpoint
    state["user"] = _user(scopes=[f"project:{_PROJECT_ID}"])

    response = client.get("/api/users/me/product-capabilities")

    assert response.status_code == 403
    assert response.json()["detail"] == "Access denied by MCP token scope"
    assert audit.await_count == 1
    assert not calls["grants"]


def test_user_scoped_mcp_token_may_query_its_own_capabilities(endpoint):
    client, state, _calls, audit, _routes = endpoint
    state["user"] = _user(scopes=["user"])

    response = client.get(
        "/api/users/me/product-capabilities",
        params={"capability_id": "jobs.create"},
    )

    assert response.status_code == 200
    assert audit.await_count == 0


def test_rollout_disabled_returns_explicit_unavailable_health_signal(endpoint):
    client, state, calls, _audit, routes = endpoint
    state["gate"] = False

    response = client.get("/api/users/me/product-capabilities")

    assert response.status_code == 503
    assert response.json() == {
        "detail": {
            "code": "product_capabilities_unavailable",
            "state": "rollout_disabled",
        }
    }
    assert response.headers[routes.CAPABILITY_PLANE_HEADER] == "rollout_disabled"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["retry-after"] == "60"
    assert "etag" not in response.headers
    assert not calls["grants"]

    thread_response = client.get(
        "/api/users/me/product-capabilities",
        params={"thread_id": _THREAD_ID},
    )
    assert thread_response.status_code == 503
    assert not calls["owner"]


@pytest.mark.parametrize(
    "params",
    [
        {"topic": "not exact"},
        {"capability_id": "INVALID"},
        {"limit": "0"},
        {"limit": "51"},
        {"thread_id": "not-a-uuid"},
    ],
)
def test_invalid_query_inputs_return_422(endpoint, params: dict[str, str]):
    client, _state, _calls, _audit, _routes = endpoint

    response = client.get(
        "/api/users/me/product-capabilities",
        params=params,
    )

    assert response.status_code == 422


def test_explicit_capability_id_count_is_bounded(endpoint):
    client, _state, _calls, _audit, _routes = endpoint
    params = [
        ("capability_id", f"feature.item-{index}")
        for index in range(MAX_EXPLICIT_CAPABILITY_IDS + 1)
    ]

    response = client.get(
        "/api/users/me/product-capabilities",
        params=params,
    )

    assert response.status_code == 422
    assert "At most 20 capability IDs" in response.json()["detail"]


def test_topic_and_id_filters_intersect_with_deterministic_order(endpoint):
    client, _state, _calls, _audit, _routes = endpoint

    response = client.get(
        "/api/users/me/product-capabilities",
        params=[
            ("topic", "email"),
            ("capability_id", "jobs.create"),
            ("capability_id", "datasources.email.send"),
            ("capability_id", "datasources.email"),
        ],
    )

    assert response.status_code == 200
    parsed = ProductCapabilitiesResponse.model_validate(response.json())
    assert [item.id for item in parsed.capabilities] == [
        "datasources.email",
        "datasources.email.send",
    ]


def test_unknown_explicit_id_returns_one_redacted_visibility_error(endpoint):
    client, _state, _calls, _audit, _routes = endpoint

    response = client.get(
        "/api/users/me/product-capabilities",
        params={"capability_id": "unknown.feature"},
    )

    assert response.status_code == 200
    parsed = ProductCapabilitiesResponse.model_validate(response.json())
    assert parsed.capabilities == ()
    assert parsed.completeness is Completeness.PARTIAL
    assert len(parsed.evaluation_errors) == 1
    error = parsed.evaluation_errors[0]
    assert error.code is EvaluationErrorCode.CAPABILITY_NOT_VISIBLE
    assert error.capability_id is None
    assert error.layer is None
    assert "unknown.feature" not in response.text


def test_limit_truncation_is_explicit_and_schema_valid(endpoint):
    client, _state, _calls, _audit, _routes = endpoint

    response = client.get(
        "/api/users/me/product-capabilities",
        params={"limit": "1"},
    )

    assert response.status_code == 200
    parsed = ProductCapabilitiesResponse.model_validate(response.json())
    assert parsed.truncated is True
    assert parsed.completeness is Completeness.PARTIAL
    assert len(parsed.capabilities) == 1
    assert parsed.evaluation_errors[0].code is EvaluationErrorCode.RESULT_LIMIT


def test_main_application_includes_the_endpoint_exactly_once():
    import main

    def effective_routes(routes):
        """Flatten both copied and lazily included FastAPI router layouts."""

        for route in routes:
            original_router = getattr(route, "original_router", None)
            if original_router is not None:
                yield from effective_routes(original_router.routes)
            else:
                yield route

    matches = [
        route
        for route in effective_routes(main.app.routes)
        if getattr(route, "path", None) == "/api/users/me/product-capabilities"
        and "GET" in getattr(route, "methods", set())
    ]
    assert len(matches) == 1


def test_operator_rollout_gate_is_wired_default_off_in_deployments():
    values = (_ROOT / "helm" / "values.yaml").read_text(encoding="utf-8")
    configmap = (_ROOT / "helm" / "templates" / "configmap.yaml").read_text(
        encoding="utf-8"
    )
    deployment = (
        _ROOT / "helm" / "templates" / "orchestrator" / "deployment.yaml"
    ).read_text(encoding="utf-8")
    env_example = (_ROOT / ".env.example").read_text(encoding="utf-8")

    assert 'productCapabilitiesEndpointEnabled: "false"' in values
    assert "PRODUCT_CAPABILITIES_ENDPOINT_ENABLED:" in configmap
    assert ".Values.agent.productCapabilitiesEndpointEnabled" in configmap
    assert "- name: PRODUCT_CAPABILITIES_ENDPOINT_ENABLED" in deployment
    assert "key: PRODUCT_CAPABILITIES_ENDPOINT_ENABLED" in deployment
    assert "PRODUCT_CAPABILITIES_ENDPOINT_ENABLED=false" in env_example
