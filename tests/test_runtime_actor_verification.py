"""Admin-only adapters for bounded Officer runtime-grant verification."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException

import orchestrator.main as main
from orchestrator.services.runtime_actor import RuntimeActorRefreshExchange
from src.shared.runtime_actor import RuntimeActorContext


PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ADMIN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PLAN_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
IDEMPOTENCY_KEY = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


def _body(**overrides):
    values = {
        "idempotency_key": UUID(IDEMPOTENCY_KEY),
        "exercise": "maintenance_failure",
        "expires_in_seconds": 900,
    }
    values.update(overrides)
    return main.OfficerRuntimeVerificationPlanRequest(**values)


@pytest.mark.asyncio
async def test_admin_plan_route_uses_only_authenticated_admin_identity(monkeypatch):
    admin = {"id": ADMIN_ID, "real_is_admin": True}
    require_admin = AsyncMock(return_value=admin)
    create = AsyncMock(
        return_value={
            "plan_id": PLAN_ID,
            "exercise": "maintenance_failure",
            "state": "armed",
            "replayed": False,
        }
    )
    audit = AsyncMock()
    monkeypatch.setattr(main, "_require_admin", require_admin)
    monkeypatch.setattr(main, "create_runtime_verification_plan", create)
    monkeypatch.setattr(main, "log_security_event", audit)
    monkeypatch.setattr(main, "OFFICER_RUNTIME_VERIFICATION_ENABLED", True)
    request = MagicMock()

    result = await main.create_officer_runtime_verification(
        PROJECT_ID, _body(), request
    )

    assert result == {
        "enabled": True,
        "plan": {
            "plan_id": PLAN_ID,
            "exercise": "maintenance_failure",
            "state": "armed",
            "replayed": False,
        },
    }
    create.assert_awaited_once_with(
        main.postgres_db,
        enabled=True,
        project_id=PROJECT_ID,
        idempotency_key=IDEMPOTENCY_KEY,
        exercise="maintenance_failure",
        created_by=ADMIN_ID,
        expires_in_seconds=900,
        logical_window_seconds=None,
        response_losses=None,
        response_loss_gap_seconds=None,
    )
    audit.assert_awaited_once_with(
        main.postgres_db,
        event_type="officer_runtime_verification_created",
        user=admin,
        resource_type="officer_runtime_verification",
        resource_id=PLAN_ID,
        detail=(
            f"project_id={PROJECT_ID} plan_id={PLAN_ID} "
            "exercise=maintenance_failure action=create replayed=false"
        ),
        request=request,
    )


@pytest.mark.asyncio
async def test_viewer_cannot_reach_plan_service(monkeypatch):
    denied = HTTPException(status_code=403, detail="Admin access required")
    monkeypatch.setattr(main, "_require_admin", AsyncMock(side_effect=denied))
    create = AsyncMock()
    monkeypatch.setattr(main, "create_runtime_verification_plan", create)

    with pytest.raises(HTTPException) as exc:
        await main.create_officer_runtime_verification(PROJECT_ID, _body(), MagicMock())

    assert exc.value.status_code == 403
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_plan_route_is_not_discoverable_as_an_active_seam(monkeypatch):
    monkeypatch.setattr(
        main,
        "_require_admin",
        AsyncMock(return_value={"id": ADMIN_ID, "real_is_admin": True}),
    )
    monkeypatch.setattr(main, "OFFICER_RUNTIME_VERIFICATION_ENABLED", False)
    monkeypatch.setattr(
        main,
        "create_runtime_verification_plan",
        AsyncMock(
            side_effect=main.RuntimeVerificationPlanError(
                "verification_disabled", "disabled", status_code=404
            )
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await main.create_officer_runtime_verification(PROJECT_ID, _body(), MagicMock())

    assert exc.value.status_code == 404
    assert exc.value.detail["code"] == "verification_disabled"


@pytest.mark.asyncio
async def test_committed_response_loss_returns_only_generic_retry(monkeypatch):
    actor = RuntimeActorContext(
        caller_kind="officer",
        project_id=PROJECT_ID,
        project_role="owner",
        thread_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
        officer_incarnation=0,
        user_id=ADMIN_ID,
        access_credential="sra_" + "A" * 43,
        refresh_credential="srr_" + "B" * 43,
    )
    exchange = RuntimeActorRefreshExchange(
        actor=actor,
        response_lost=True,
        verification_plan_id=PLAN_ID,
    )
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(
        main, "refresh_runtime_actor_exchange", AsyncMock(return_value=exchange)
    )
    kick = MagicMock()
    monkeypatch.setattr(main, "_kick_officer_event_drain", kick)
    monkeypatch.setattr(main, "OFFICER_RUNTIME_VERIFICATION_ENABLED", True)

    with pytest.raises(HTTPException) as exc:
        await main.refresh_runtime_actor(MagicMock())

    assert exc.value.status_code == 503
    assert exc.value.detail == {
        "code": "runtime_maintenance_unavailable",
        "retryable": True,
    }
    assert PLAN_ID not in str(exc.value.detail)
    assert actor.access_credential not in str(exc.value.detail)
    assert actor.refresh_credential not in str(exc.value.detail)
    kick.assert_not_called()


@pytest.mark.asyncio
async def test_recover_transition_is_admin_only_and_exact_plan(monkeypatch):
    admin = {"id": ADMIN_ID, "real_is_admin": True}
    transition = AsyncMock(
        return_value={
            "plan_id": PLAN_ID,
            "exercise": "maintenance_failure",
            "state": "recovering",
            "replayed": False,
        }
    )
    audit = AsyncMock()
    monkeypatch.setattr(
        main,
        "_require_admin",
        AsyncMock(return_value=admin),
    )
    monkeypatch.setattr(main, "transition_runtime_verification_plan", transition)
    monkeypatch.setattr(main, "log_security_event", audit)
    monkeypatch.setattr(main, "OFFICER_RUNTIME_VERIFICATION_ENABLED", True)
    request = MagicMock()

    result = await main.transition_officer_runtime_verification(
        PROJECT_ID, PLAN_ID, "recover", request
    )

    assert result["plan"]["state"] == "recovering"
    transition.assert_awaited_once_with(
        main.postgres_db,
        enabled=True,
        project_id=PROJECT_ID,
        plan_id=PLAN_ID,
        action="recover",
        actor_id=ADMIN_ID,
    )
    audit.assert_awaited_once_with(
        main.postgres_db,
        event_type="officer_runtime_verification_recovery_requested",
        user=admin,
        resource_type="officer_runtime_verification",
        resource_id=PLAN_ID,
        detail=(
            f"project_id={PROJECT_ID} plan_id={PLAN_ID} "
            "exercise=maintenance_failure action=recover replayed=false"
        ),
        request=request,
    )


@pytest.mark.asyncio
async def test_transition_retry_after_lost_response_is_audited_as_replay(monkeypatch):
    admin = {"id": ADMIN_ID, "real_is_admin": True}
    immutable = {
        "plan_id": PLAN_ID,
        "exercise": "maintenance_failure",
        "state": "recovering",
        "recovery_requested_by": ADMIN_ID,
        "recovery_requested_at": "2026-08-21T12:00:00+00:00",
    }
    transition = AsyncMock(
        side_effect=[
            {**immutable, "replayed": False},
            {**immutable, "replayed": True},
        ]
    )
    audit = AsyncMock()
    monkeypatch.setattr(main, "_require_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(main, "transition_runtime_verification_plan", transition)
    monkeypatch.setattr(main, "log_security_event", audit)
    monkeypatch.setattr(main, "OFFICER_RUNTIME_VERIFICATION_ENABLED", True)

    # The first successful HTTP result is deliberately discarded.
    await main.transition_officer_runtime_verification(
        PROJECT_ID, PLAN_ID, "recover", MagicMock()
    )
    retry = await main.transition_officer_runtime_verification(
        PROJECT_ID, PLAN_ID, "recover", MagicMock()
    )

    assert retry["plan"] == {**immutable, "replayed": True}
    assert transition.await_count == 2
    assert all(
        call.kwargs["actor_id"] == ADMIN_ID for call in transition.await_args_list
    )
    assert "replayed=true" in audit.await_args_list[-1].kwargs["detail"]


@pytest.mark.asyncio
async def test_disarm_success_emits_attributed_security_event(monkeypatch):
    admin = {"id": ADMIN_ID, "real_is_admin": True}
    transition = AsyncMock(
        return_value={
            "plan_id": PLAN_ID,
            "exercise": "response_loss",
            "state": "disarmed",
            "disarmed_by": ADMIN_ID,
            "disarmed_at": "2026-08-21T12:00:00+00:00",
            "replayed": False,
        }
    )
    audit = AsyncMock()
    monkeypatch.setattr(main, "_require_admin", AsyncMock(return_value=admin))
    monkeypatch.setattr(main, "transition_runtime_verification_plan", transition)
    monkeypatch.setattr(main, "log_security_event", audit)
    monkeypatch.setattr(main, "OFFICER_RUNTIME_VERIFICATION_ENABLED", True)
    request = MagicMock()

    result = await main.transition_officer_runtime_verification(
        PROJECT_ID, PLAN_ID, "disarm", request
    )

    assert result["plan"]["disarmed_by"] == ADMIN_ID
    transition.assert_awaited_once_with(
        main.postgres_db,
        enabled=True,
        project_id=PROJECT_ID,
        plan_id=PLAN_ID,
        action="disarm",
        actor_id=ADMIN_ID,
    )
    audit.assert_awaited_once_with(
        main.postgres_db,
        event_type="officer_runtime_verification_disarmed",
        user=admin,
        resource_type="officer_runtime_verification",
        resource_id=PLAN_ID,
        detail=(
            f"project_id={PROJECT_ID} plan_id={PLAN_ID} "
            "exercise=response_loss action=disarm replayed=false"
        ),
        request=request,
    )


@pytest.mark.asyncio
async def test_idempotency_conflict_is_stable_409_and_not_success_audited(monkeypatch):
    audit = AsyncMock()
    monkeypatch.setattr(
        main,
        "_require_admin",
        AsyncMock(return_value={"id": ADMIN_ID, "real_is_admin": True}),
    )
    monkeypatch.setattr(
        main,
        "create_runtime_verification_plan",
        AsyncMock(
            side_effect=main.RuntimeVerificationPlanError(
                "idempotency_conflict", "different request"
            )
        ),
    )
    monkeypatch.setattr(main, "log_security_event", audit)
    monkeypatch.setattr(main, "OFFICER_RUNTIME_VERIFICATION_ENABLED", True)

    with pytest.raises(HTTPException) as exc:
        await main.create_officer_runtime_verification(PROJECT_ID, _body(), MagicMock())

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "idempotency_conflict"
    audit.assert_not_awaited()
