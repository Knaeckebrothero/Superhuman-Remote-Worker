"""HTTP adapters for completion-command operator recovery verbs."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import orchestrator.main as main
from orchestrator.services.completion_command_resolution import (
    CompletionForceResolveResult,
    CompletionResolutionConflict,
    CompletionResolutionNotFound,
    CompletionUnparkResult,
)


COMMAND_ID = "22222222-bbbb-4222-8222-222222222222"
JOB_ID = "11111111-aaaa-4111-8111-111111111111"
ADMIN_ID = "33333333-cccc-4333-8333-333333333333"


@pytest.fixture
def operator(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    service = MagicMock()
    service.unpark = AsyncMock()
    service.force_resolve = AsyncMock()
    monkeypatch.setattr(
        main, "_require_admin", AsyncMock(return_value={"id": ADMIN_ID})
    )
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "_get_completion_command_resolution", lambda: service)
    return service


@pytest.mark.asyncio
async def test_admin_unpark_delegates_exact_command_and_serializes_deadline(
    operator: MagicMock,
) -> None:
    deadline = datetime(2026, 8, 14, tzinfo=UTC)
    operator.unpark.return_value = CompletionUnparkResult(
        command_id=COMMAND_ID,
        job_id=JOB_ID,
        report_seq=7,
        state="pending",
        reset_effects=("subjob_output_graft",),
        deadline_at=deadline,
    )

    result = await main.admin_completion_command_unpark(COMMAND_ID, MagicMock())

    operator.unpark.assert_awaited_once_with(main.UUID(COMMAND_ID), actor=ADMIN_ID)
    assert result == {
        "command_id": COMMAND_ID,
        "job_id": JOB_ID,
        "report_seq": 7,
        "state": "pending",
        "reset_effects": ("subjob_output_graft",),
        "deadline_at": deadline,
    }


@pytest.mark.asyncio
async def test_admin_force_resolve_prunes_checkpoint_after_durable_commit(
    operator: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = {
        "status": "force_resolved",
        "incident": True,
        "callbacks": False,
    }
    operator.force_resolve.return_value = CompletionForceResolveResult(
        command_id=COMMAND_ID,
        job_id=JOB_ID,
        report_seq=8,
        state="force_resolved",
        terminal_status="failed",
        prior_job_status="processing",
        abandoned_effects=("workspace_archive_teardown",),
        outcome=outcome,
    )
    prune = AsyncMock()
    monkeypatch.setattr(main.postgres_db, "delete_checkpoint_thread", prune)
    body = main.CompletionCommandForceResolveRequest(
        expected_state="parked",
        terminal_status="failed",
        reason="operator confirmed delivery cannot converge",
    )

    result = await main.admin_completion_command_force_resolve(
        COMMAND_ID, body, MagicMock()
    )

    operator.force_resolve.assert_awaited_once_with(
        main.UUID(COMMAND_ID),
        expected_state="parked",
        terminal_status="failed",
        actor=ADMIN_ID,
        reason="operator confirmed delivery cannot converge",
    )
    prune.assert_awaited_once_with(JOB_ID)
    assert result["state"] == "force_resolved"
    assert result["abandoned_effects"] == ("workspace_archive_teardown",)
    assert result["outcome"] == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "error", "status", "detail"),
    [
        (
            "unpark",
            CompletionResolutionNotFound(COMMAND_ID),
            404,
            "Completion command not found",
        ),
        (
            "unpark",
            CompletionResolutionConflict("command_owner_live"),
            409,
            "command_owner_live",
        ),
        (
            "force_resolve",
            CompletionResolutionConflict("workspace_teardown_authorized"),
            409,
            "workspace_teardown_authorized",
        ),
    ],
)
async def test_admin_operator_errors_have_stable_http_status(
    operator: MagicMock,
    operation: str,
    error: Exception,
    status: int,
    detail: str,
) -> None:
    getattr(operator, operation).side_effect = error

    with pytest.raises(HTTPException) as exc:
        if operation == "unpark":
            await main.admin_completion_command_unpark(COMMAND_ID, MagicMock())
        else:
            await main.admin_completion_command_force_resolve(
                COMMAND_ID,
                main.CompletionCommandForceResolveRequest(
                    expected_state="parked",
                    terminal_status="completed",
                    reason="incident resolution",
                ),
                MagicMock(),
            )

    assert exc.value.status_code == status
    assert exc.value.detail == detail


@pytest.mark.asyncio
async def test_invalid_command_id_is_404_after_admin_authorization(
    operator: MagicMock,
) -> None:
    with pytest.raises(HTTPException) as exc:
        await main.admin_completion_command_unpark("not-a-uuid", MagicMock())

    assert exc.value.status_code == 404
    operator.unpark.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["unpark", "force_resolve"])
async def test_commands_off_authorizes_then_stays_service_dark(
    operator: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", False)

    with pytest.raises(HTTPException) as exc:
        if operation == "unpark":
            await main.admin_completion_command_unpark(COMMAND_ID, MagicMock())
        else:
            await main.admin_completion_command_force_resolve(
                COMMAND_ID,
                main.CompletionCommandForceResolveRequest(
                    expected_state="parked",
                    terminal_status="completed",
                    reason="must remain dark",
                ),
                MagicMock(),
            )

    assert exc.value.status_code == 404
    assert exc.value.detail == "Completion commands are disabled"
    operator.unpark.assert_not_awaited()
    operator.force_resolve.assert_not_awaited()


@pytest.mark.parametrize("reorder_enabled", [False, True])
def test_safety_preclaim_and_router_reconciliation_follow_reorder_gate(
    monkeypatch: pytest.MonkeyPatch,
    reorder_enabled: bool,
) -> None:
    monkeypatch.setattr(main, "COMPLETION_STATUS_REORDER_ENABLED", reorder_enabled)
    monkeypatch.setattr(main, "_completion_finalizer_instance", None)
    monkeypatch.setattr(main, "_completion_sweep_router_instance", None)
    monkeypatch.setattr(main, "_completion_command_resolution_instance", None)

    finalizer = main._get_completion_finalizer()
    router = main._get_completion_sweep_router()

    if reorder_enabled:
        resolution = main._completion_command_resolution_instance
        assert resolution is not None
        assert finalizer.preclaim.__self__ is resolution
        assert router.safety_net is resolution
    else:
        assert finalizer.preclaim is None
        assert router.safety_net is None
        assert main._completion_command_resolution_instance is None
