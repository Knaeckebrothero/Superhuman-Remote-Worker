"""HTTP contracts for the Gate-3 ``/complete`` admission wrapper.

These tests deliberately stub the legacy completion body.  Its side effects
remain covered by the existing endpoint suites; this file proves only the dark
gate, admission ordering, durable outcome handoff, and replay response matrix.
"""

from __future__ import annotations

import builtins
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from starlette.responses import JSONResponse

import main
from services import job_completion_commands as commands


JOB_ID = "11111111-2222-3333-4444-555555555555"
AGENT_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
REPORT_ID = UUID("99999999-8888-7777-6666-555555555555")
COMMAND_ID = "12345678-1234-5678-9abc-123456789abc"


def _body() -> main.JobCompleteRequest:
    return main.JobCompleteRequest(
        should_stop=True,
        goal_achieved=True,
        error=None,
        freeze_data={"freeze_type": "job_complete", "summary": "done"},
        lease_token=17,
        agent_id=AGENT_ID,
        client_report_id=REPORT_ID,
    )


def _accepted(
    disposition: str,
    *,
    state: str = "pending",
    outcome: dict | None = None,
    winning_report_seq: int | None = None,
    abandoned_effects: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        disposition=disposition,
        command_id=COMMAND_ID,
        job_id=JOB_ID,
        report_seq=3,
        state=state,
        stored_payload={},
        outcome=outcome,
        winning_report_seq=winning_report_seq,
        abandoned_effects=abandoned_effects,
        client_report_id=str(REPORT_ID),
        queue_terminalized=False,
    )


def _response_json(response: JSONResponse) -> dict:
    return json.loads(response.body.decode("utf-8"))


@pytest.mark.asyncio
async def test_default_off_bypasses_command_module_and_preserves_legacy_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The closed gate must be byte-for-byte legacy at the wrapper boundary."""

    request = MagicMock()
    body = _body()
    legacy_result = {
        "status": "success",
        "job_id": JOB_ID,
        "new_status": "completed",
        "actions": ["legacy action"],
    }
    legacy = AsyncMock(return_value=legacy_result)
    accept = AsyncMock(side_effect=AssertionError("accept must remain dark"))
    settle = AsyncMock(side_effect=AssertionError("settle must remain dark"))
    auth = AsyncMock()

    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", False)
    monkeypatch.setattr(main, "require_internal", auth)
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)
    monkeypatch.setattr(commands, "complete_completion_command", settle)

    original_import = builtins.__import__

    def reject_command_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "services.job_completion_commands":
            raise AssertionError("closed gate attempted command-service import")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_command_import)

    handled = await main.complete_job(request, JOB_ID, body)

    assert handled is legacy_result
    auth.assert_awaited_once_with(request)
    legacy.assert_awaited_once_with(request, JOB_ID, body, _authorized=True)
    accept.assert_not_awaited()
    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_admission_precedes_legacy_and_settles_exact_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = MagicMock()
    body = _body()
    database = object()
    events: list[str] = []
    legacy_result = {
        "status": "success",
        "job_id": JOB_ID,
        "new_status": "completed",
        "actions": ["effect-a", "effect-b"],
    }

    async def auth(_request) -> None:
        events.append("authenticate")

    async def accept(*args, **kwargs):
        events.append("accept")
        return _accepted("fresh")

    async def legacy(*args, **kwargs):
        events.append("legacy")
        return legacy_result

    async def settle(*args, **kwargs):
        events.append("settle")
        return True

    accept_mock = AsyncMock(side_effect=accept)
    legacy_mock = AsyncMock(side_effect=legacy)
    settle_mock = AsyncMock(side_effect=settle)
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "postgres_db", database)
    monkeypatch.setattr(main, "require_internal", AsyncMock(side_effect=auth))
    monkeypatch.setattr(main, "_complete_job_legacy", legacy_mock)
    monkeypatch.setattr(commands, "accept_completion_command", accept_mock)
    monkeypatch.setattr(commands, "complete_completion_command", settle_mock)

    handled = await main.complete_job(request, JOB_ID, body)

    assert handled is legacy_result
    assert events == ["authenticate", "accept", "legacy", "settle"]
    legacy_mock.assert_awaited_once_with(request, JOB_ID, body, _authorized=True)
    settle_mock.assert_awaited_once_with(database, COMMAND_ID, legacy_result)
    accept_mock.assert_awaited_once_with(
        database,
        job_id=JOB_ID,
        payload={
            "should_stop": True,
            "goal_achieved": True,
            "error": None,
            "freeze_data": {
                "freeze_type": "job_complete",
                "summary": "done",
            },
        },
        lease_token=17,
        agent_id=str(AGENT_ID),
        client_report_id=str(REPORT_ID),
        requested_by=f"agent:{AGENT_ID}",
    )


@pytest.mark.asyncio
async def test_done_replay_returns_stored_outcome_with_idempotency_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = {
        "status": "success",
        "job_id": JOB_ID,
        "new_status": "completed",
        "actions": ["already finalized"],
    }
    accept = AsyncMock(
        return_value=_accepted("replay_done", state="done", outcome=outcome)
    )
    legacy = AsyncMock()
    settle = AsyncMock()
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)
    monkeypatch.setattr(commands, "complete_completion_command", settle)

    response = await main.complete_job(MagicMock(), JOB_ID, _body())

    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert response.headers["Idempotent-Replayed"] == "true"
    assert _response_json(response) == outcome
    legacy.assert_not_awaited()
    settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_replay_is_retryable_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accept = AsyncMock(side_effect=commands.CompletionInProgress(COMMAND_ID, "pending"))
    legacy = AsyncMock()
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)

    with pytest.raises(HTTPException) as caught:
        await main.complete_job(MagicMock(), JOB_ID, _body())

    assert caught.value.status_code == 409
    assert caught.value.headers == {"Retry-After": "1"}
    assert "pending" in str(caught.value.detail)
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_divergent_replay_is_unprocessable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accept = AsyncMock(
        side_effect=commands.CompletionPayloadMismatch(
            "client_report_id was reused with a different payload"
        )
    )
    legacy = AsyncMock()
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)

    with pytest.raises(HTTPException) as caught:
        await main.complete_job(MagicMock(), JOB_ID, _body())

    assert caught.value.status_code == 422
    assert caught.value.headers is None
    assert "different payload" in str(caught.value.detail)
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_parked_replay_is_accepted_without_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accept = AsyncMock(return_value=_accepted("replay_parked", state="parked"))
    legacy = AsyncMock()
    settle = AsyncMock()
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)
    monkeypatch.setattr(commands, "complete_completion_command", settle)

    response = await main.complete_job(MagicMock(), JOB_ID, _body())

    assert isinstance(response, JSONResponse)
    assert response.status_code == 202
    assert "Retry-After" not in response.headers
    assert response.headers["Idempotent-Replayed"] == "true"
    assert _response_json(response) == {
        "status": "still_pending",
        "job_id": JOB_ID,
        "command_id": COMMAND_ID,
        "command_state": "parked",
    }
    legacy.assert_not_awaited()
    settle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "state", "outcome", "winning_seq", "abandoned"),
    [
        (
            "replay_superseded",
            "superseded",
            {
                "status": "superseded",
                "job_id": JOB_ID,
                "winning_report_seq": 2,
            },
            2,
            (),
        ),
        (
            "replay_force_resolved",
            "force_resolved",
            {
                "status": "force_resolved",
                "job_id": JOB_ID,
                "abandoned_effects": ["workspace_cleanup"],
            },
            None,
            ("workspace_cleanup",),
        ),
    ],
)
async def test_operator_terminal_replays_return_their_durable_outcome(
    monkeypatch: pytest.MonkeyPatch,
    disposition: str,
    state: str,
    outcome: dict,
    winning_seq: int | None,
    abandoned: tuple[str, ...],
) -> None:
    accept = AsyncMock(
        return_value=_accepted(
            disposition,
            state=state,
            outcome=outcome,
            winning_report_seq=winning_seq,
            abandoned_effects=abandoned,
        )
    )
    legacy = AsyncMock()
    settle = AsyncMock()
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)
    monkeypatch.setattr(commands, "complete_completion_command", settle)

    response = await main.complete_job(MagicMock(), JOB_ID, _body())

    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert response.headers["Idempotent-Replayed"] == "true"
    assert _response_json(response) == outcome
    assert "Retry-After" not in response.headers
    legacy.assert_not_awaited()
    settle.assert_not_awaited()
