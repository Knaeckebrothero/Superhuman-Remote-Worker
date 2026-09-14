"""Wire-level tests for the extracted runtime-actor / Officer verification routes.

R1.B06 lane C, census group ``J_runtime_verification``. Every test drives the
router mounted on a bare ``FastAPI()`` so what is asserted is the HTTP contract
— status code and body — rather than a Python return value.

The refusal shapes here are the point (port contract §P7): both refresh faults
collapse to one indistinguishable 503, and a disabled feature is a 404 carrying
``{"code", "message"}``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from orchestrator.routers import officer_runtime_verification as router_mod
from orchestrator.security.access import require_internal as real_require_internal
from orchestrator.services.officer_runtime_verification import (
    OfficerRuntimeVerificationDependencies,
)
from orchestrator.services.runtime_actor_verification import (
    RuntimeVerificationPlanError,
)

PROJECT_ID = "11111111-1111-4111-8111-111111111111"
PLAN_ID = "22222222-2222-4222-8222-222222222222"
ADMIN_ID = "33333333-3333-4333-8333-333333333333"
IDEMPOTENCY_KEY = "44444444-4444-4444-8444-444444444444"


def _plan(**over):
    return {
        "plan_id": PLAN_ID,
        "exercise": "longevity",
        "state": "armed",
        "replayed": False,
        **over,
    }


def _deps(**over) -> OfficerRuntimeVerificationDependencies:
    base = dict(
        store=MagicMock(name="postgres_db"),
        logger=MagicMock(),
        require_internal=AsyncMock(return_value=None),
        require_admin=AsyncMock(return_value={"id": ADMIN_ID}),
        log_security_event=AsyncMock(),
        officer_runtime_verification_enabled=lambda: True,
        authorize_runtime_actor_request=AsyncMock(),
        refresh_runtime_actor_exchange=AsyncMock(),
        create_runtime_verification_plan=AsyncMock(return_value=_plan()),
        get_runtime_verification_plan=AsyncMock(return_value=_plan()),
        transition_runtime_verification_plan=AsyncMock(
            return_value=_plan(state="recovering")
        ),
        kick_officer_event_drain=MagicMock(),
    )
    base.update(over)
    return OfficerRuntimeVerificationDependencies(**base)


def _client(dependencies) -> TestClient:
    app = FastAPI()
    app.state.officer_runtime_verification_dependencies_factory = lambda: dependencies
    app.include_router(router_mod.router)
    return TestClient(app, raise_server_exceptions=False)


def _body(**over):
    return {"idempotency_key": IDEMPOTENCY_KEY, "exercise": "longevity", **over}


# --- transport guard ---------------------------------------------------------


def test_authorize_without_internal_key_is_401():
    """The real guard fails closed: no configured key, no caller."""
    deps = _deps(require_internal=real_require_internal)
    resp = _client(deps).post(
        "/api/runtime-actors/authorize",
        json={"action": "charter", "project_id": PROJECT_ID},
    )
    assert resp.status_code == 401
    assert resp.json() == {"detail": "Invalid internal key"}
    deps.authorize_runtime_actor_request.assert_not_awaited()


def test_refresh_without_internal_key_is_401():
    deps = _deps(require_internal=real_require_internal)
    resp = _client(deps).post("/api/runtime-actors/refresh")
    assert resp.status_code == 401
    deps.refresh_runtime_actor_exchange.assert_not_awaited()


def test_authorize_returns_actor_audit_payload():
    actor = SimpleNamespace(
        audit_payload=lambda: {"kind": "officer", "project_id": PROJECT_ID}
    )
    deps = _deps(authorize_runtime_actor_request=AsyncMock(return_value=actor))
    resp = _client(deps).post(
        "/api/runtime-actors/authorize",
        json={"action": "machine_tags", "project_id": PROJECT_ID},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "authorized": True,
        "code": "authorized",
        "action": "machine_tags",
        "actor": {"kind": "officer", "project_id": PROJECT_ID},
        "message": "Runtime actor is authorized.",
    }


def test_authorize_rejects_unknown_action_before_the_service():
    deps = _deps()
    resp = _client(deps).post(
        "/api/runtime-actors/authorize",
        json={"action": "delete_everything", "project_id": PROJECT_ID},
    )
    assert resp.status_code == 422
    deps.authorize_runtime_actor_request.assert_not_awaited()


# --- refresh: two faults, one indistinguishable body -------------------------


@pytest.mark.parametrize(
    "exchange",
    [
        SimpleNamespace(
            retryable_failure_code="maintenance", response_lost=False, actor=None
        ),
        SimpleNamespace(
            retryable_failure_code=None,
            response_lost=True,
            actor=SimpleNamespace(to_payload=lambda: {"secret": "leak"}),
        ),
    ],
    ids=["refused_before_mutation", "committed_but_withheld"],
)
def test_refresh_faults_share_one_opaque_503(exchange):
    """Telling the two apart on the wire would leak that a plan exists."""
    deps = _deps(refresh_runtime_actor_exchange=AsyncMock(return_value=exchange))
    resp = _client(deps).post("/api/runtime-actors/refresh")
    assert resp.status_code == 503
    assert resp.json() == {
        "detail": {"code": "runtime_maintenance_unavailable", "retryable": True}
    }
    assert "secret" not in resp.text


def test_refresh_kicks_the_officer_drain_only_for_an_officer():
    officer = SimpleNamespace(
        retryable_failure_code=None,
        response_lost=False,
        caller_kind="officer",
        actor=None,
    )
    officer.actor = SimpleNamespace(
        caller_kind="officer", to_payload=lambda: {"token": "t"}
    )
    deps = _deps(refresh_runtime_actor_exchange=AsyncMock(return_value=officer))
    resp = _client(deps).post("/api/runtime-actors/refresh")
    assert resp.status_code == 200
    assert resp.json() == {"runtime_actor": {"token": "t"}}
    deps.kick_officer_event_drain.assert_called_once_with(deps.store)

    worker = SimpleNamespace(
        retryable_failure_code=None,
        response_lost=False,
        actor=SimpleNamespace(caller_kind="worker", to_payload=lambda: {"token": "w"}),
    )
    deps2 = _deps(refresh_runtime_actor_exchange=AsyncMock(return_value=worker))
    assert _client(deps2).post("/api/runtime-actors/refresh").status_code == 200
    deps2.kick_officer_event_drain.assert_not_called()


def test_refresh_without_an_actor_is_a_defensive_503():
    exchange = SimpleNamespace(
        retryable_failure_code=None, response_lost=False, actor=None
    )
    deps = _deps(refresh_runtime_actor_exchange=AsyncMock(return_value=exchange))
    resp = _client(deps).post("/api/runtime-actors/refresh")
    assert resp.status_code == 503
    assert resp.json() == {"detail": "Runtime maintenance unavailable"}


# --- plan create / read / transition ----------------------------------------


def test_create_plan_audits_after_the_write_and_echoes_the_plan():
    deps = _deps()
    resp = _client(deps).post(
        f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification",
        json=_body(expires_in_seconds=300),
    )
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "plan": _plan()}
    deps.create_runtime_verification_plan.assert_awaited_once()
    kwargs = deps.create_runtime_verification_plan.await_args.kwargs
    assert kwargs["enabled"] is True
    assert kwargs["project_id"] == PROJECT_ID
    assert kwargs["created_by"] == ADMIN_ID
    assert kwargs["expires_in_seconds"] == 300
    deps.log_security_event.assert_awaited_once()
    audit = deps.log_security_event.await_args.kwargs
    assert audit["event_type"] == "officer_runtime_verification_created"
    assert audit["resource_id"] == PLAN_ID
    assert "replayed=false" in audit["detail"]


def test_create_plan_refuses_an_unbounded_window_before_the_service():
    """``expires_in_seconds`` bounds are the request contract, not advice."""
    deps = _deps()
    resp = _client(deps).post(
        f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification",
        json=_body(expires_in_seconds=86_400),
    )
    assert resp.status_code == 422
    deps.create_runtime_verification_plan.assert_not_awaited()


def test_admin_refusal_never_reaches_the_plan_service():
    deps = _deps(
        require_admin=AsyncMock(
            side_effect=HTTPException(status_code=403, detail="Admin required")
        )
    )
    resp = _client(deps).post(
        f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification",
        json=_body(),
    )
    assert resp.status_code == 403
    deps.create_runtime_verification_plan.assert_not_awaited()
    deps.log_security_event.assert_not_awaited()


def test_disabled_feature_is_a_404_with_the_plan_error_shape():
    deps = _deps(
        create_runtime_verification_plan=AsyncMock(
            side_effect=RuntimeVerificationPlanError(
                "verification_disabled", "Disabled", status_code=404
            )
        )
    )
    resp = _client(deps).post(
        f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification",
        json=_body(),
    )
    assert resp.status_code == 404
    assert resp.json() == {
        "detail": {"code": "verification_disabled", "message": "Disabled"}
    }
    deps.log_security_event.assert_not_awaited()


def test_read_plan_is_admin_only_and_never_audits():
    deps = _deps()
    resp = _client(deps).get(
        f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification"
    )
    assert resp.status_code == 200
    assert resp.json() == {"enabled": True, "plan": _plan()}
    deps.log_security_event.assert_not_awaited()


@pytest.mark.parametrize(
    "action,event",
    [
        ("recover", "officer_runtime_verification_recovery_requested"),
        ("disarm", "officer_runtime_verification_disarmed"),
    ],
)
def test_transition_maps_each_action_to_its_own_audit_event(action, event):
    deps = _deps()
    resp = _client(deps).post(
        f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification/"
        f"{PLAN_ID}/{action}"
    )
    assert resp.status_code == 200
    kwargs = deps.transition_runtime_verification_plan.await_args.kwargs
    assert kwargs["plan_id"] == PLAN_ID
    assert kwargs["action"] == action
    assert kwargs["actor_id"] == ADMIN_ID
    assert deps.log_security_event.await_args.kwargs["event_type"] == event


def test_transition_rejects_an_action_outside_the_closed_vocabulary():
    deps = _deps()
    resp = _client(deps).post(
        f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification/"
        f"{PLAN_ID}/detonate"
    )
    assert resp.status_code == 422
    deps.transition_runtime_verification_plan.assert_not_awaited()


def test_replayed_transition_is_recorded_as_replayed():
    deps = _deps(
        transition_runtime_verification_plan=AsyncMock(
            return_value=_plan(state="recovering", replayed=True)
        )
    )
    resp = _client(deps).post(
        f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification/"
        f"{PLAN_ID}/recover"
    )
    assert resp.status_code == 200
    assert "replayed=true" in deps.log_security_event.await_args.kwargs["detail"]


def test_flag_is_read_per_request_not_captured_at_import():
    """Port contract §P1: the enabled flag arrives as a callable."""
    enabled = {"value": True}
    deps = _deps(officer_runtime_verification_enabled=lambda: enabled["value"])
    client = _client(deps)
    client.get(f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification")
    assert deps.get_runtime_verification_plan.await_args.kwargs["enabled"] is True
    enabled["value"] = False
    client.get(f"/api/admin/projects/{PROJECT_ID}/officer/runtime-verification")
    assert deps.get_runtime_verification_plan.await_args.kwargs["enabled"] is False
