from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from orchestrator.schemas.job_runtime import JobCompleteRequest
from orchestrator.services import job_completion
from orchestrator.services.job_completion_commands import CompletionInProgress


JOB_ID = "10000000-0000-0000-0000-000000000001"
COMMAND_ID = "20000000-0000-0000-0000-000000000002"
AGENT_ID = UUID("30000000-0000-0000-0000-000000000003")
REPORT_ID = UUID("40000000-0000-0000-0000-000000000004")


def _body() -> JobCompleteRequest:
    return JobCompleteRequest(
        should_stop=True,
        goal_achieved=True,
        lease_token=7,
        agent_id=AGENT_ID,
        client_report_id=REPORT_ID,
    )


def _accepted(
    disposition: str,
    *,
    state: str = "pending",
    outcome: dict | None = None,
    queue_terminalized: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        disposition=disposition,
        state=state,
        outcome=outcome,
        queue_terminalized=queue_terminalized,
        job_id=JOB_ID,
        command_id=COMMAND_ID,
        winning_report_seq=9,
        abandoned_effects=("workspace_archive_teardown",),
    )


def _dependencies(
    *,
    commands_enabled: bool,
    accept: AsyncMock | None = None,
    finalizer: MagicMock | None = None,
    legacy: AsyncMock | None = None,
    auth: AsyncMock | None = None,
) -> job_completion.JobCompletionDependencies:
    return job_completion.JobCompletionDependencies(
        store=object(),
        require_internal=auth or AsyncMock(),
        commands_enabled=lambda: commands_enabled,
        status_reorder_enabled=lambda: True,
        inline_delay_seconds=lambda: 0.0,
        accept_command=accept or AsyncMock(),
        finalizer=finalizer or MagicMock(),
        legacy_complete=legacy or AsyncMock(),
        logger=logging.getLogger("test.job_completion"),
        sleep=AsyncMock(),
    )


def _response_json(response: JSONResponse) -> dict:
    return json.loads(response.body)


@pytest.mark.asyncio
async def test_flag_disabled_uses_legacy_without_command_storage() -> None:
    legacy_result = {"status": "handled", "new_status": "completed"}
    legacy = AsyncMock(return_value=legacy_result)
    accept = AsyncMock(side_effect=AssertionError("command storage was touched"))
    finalizer = MagicMock(side_effect=AssertionError("finalizer was constructed"))
    auth = AsyncMock()
    dependencies = _dependencies(
        commands_enabled=False,
        accept=accept,
        finalizer=finalizer,
        legacy=legacy,
        auth=auth,
    )
    request = MagicMock()
    body = _body()

    result = await job_completion.complete_job(
        request,
        JOB_ID,
        body,
        dependencies=dependencies,
    )

    assert result is legacy_result
    auth.assert_awaited_once_with(request)
    accept.assert_not_awaited()
    finalizer.assert_not_called()
    legacy.assert_awaited_once_with(request, JOB_ID, body, _authorized=True)


@pytest.mark.asyncio
async def test_queue_terminalized_fresh_report_returns_accepted_pending() -> None:
    accept = AsyncMock(return_value=_accepted("fresh", queue_terminalized=True))
    legacy = AsyncMock(side_effect=AssertionError("queue-owned work ran inline"))
    finalizer = MagicMock(side_effect=AssertionError("finalizer was constructed"))
    dependencies = _dependencies(
        commands_enabled=True,
        accept=accept,
        finalizer=finalizer,
        legacy=legacy,
    )

    response = await job_completion.complete_job(
        MagicMock(),
        JOB_ID,
        _body(),
        dependencies=dependencies,
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 202
    assert _response_json(response) == {
        "status": "accepted_pending",
        "job_id": JOB_ID,
        "command_id": COMMAND_ID,
        "command_state": "pending",
    }
    legacy.assert_not_awaited()
    finalizer.assert_not_called()
    assert accept.await_args.kwargs["status_reorder_enabled"] is True
    assert accept.await_args.kwargs["agent_id"] == str(AGENT_ID)
    assert accept.await_args.kwargs["client_report_id"] == str(REPORT_ID)


@pytest.mark.asyncio
async def test_done_replay_returns_stored_outcome_with_header() -> None:
    outcome = {"status": "handled", "new_status": "completed"}
    dependencies = _dependencies(
        commands_enabled=True,
        accept=AsyncMock(
            return_value=_accepted("replay_done", state="done", outcome=outcome)
        ),
    )

    response = await job_completion.complete_job(
        MagicMock(),
        JOB_ID,
        _body(),
        dependencies=dependencies,
    )

    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert response.headers["Idempotent-Replayed"] == "true"
    assert _response_json(response) == outcome


@pytest.mark.asyncio
async def test_in_progress_refusal_keeps_retry_after_contract() -> None:
    dependencies = _dependencies(
        commands_enabled=True,
        accept=AsyncMock(side_effect=CompletionInProgress(COMMAND_ID, "finalizing")),
    )

    with pytest.raises(HTTPException) as caught:
        await job_completion.complete_job(
            MagicMock(),
            JOB_ID,
            _body(),
            dependencies=dependencies,
        )

    assert caught.value.status_code == 409
    assert caught.value.headers == {"Retry-After": "1"}
    assert "finalizing" in str(caught.value.detail)


@pytest.mark.asyncio
async def test_deterministic_http_refusal_is_materialized_for_exact_replay() -> None:
    refusal = HTTPException(
        status_code=422,
        detail={"reason": "stable guard"},
        headers={"X-Completion-Guard": "true"},
    )
    legacy = AsyncMock(side_effect=refusal)
    stored: dict[str, dict] = {}

    async def finalize(_command_id: str, *, callback, inline: bool):
        assert inline is True
        stored["outcome"] = await callback(object())
        return SimpleNamespace(
            disposition="done",
            state="done",
            outcome=stored["outcome"],
        )

    dependencies = _dependencies(
        commands_enabled=True,
        accept=AsyncMock(return_value=_accepted("fresh")),
        finalizer=MagicMock(
            return_value=SimpleNamespace(
                finalize_command=AsyncMock(side_effect=finalize)
            )
        ),
        legacy=legacy,
    )

    with pytest.raises(HTTPException) as caught:
        await job_completion.complete_job(
            MagicMock(),
            JOB_ID,
            _body(),
            dependencies=dependencies,
        )

    assert caught.value.status_code == 422
    assert caught.value.detail == {"reason": "stable guard"}
    assert caught.value.headers == {"X-Completion-Guard": "true"}
    assert stored["outcome"] == {
        job_completion.DURABLE_COMPLETION_HTTP_ERROR: {
            "status_code": 422,
            "detail": {"reason": "stable guard"},
            "headers": {"X-Completion-Guard": "true"},
        }
    }


@pytest.mark.asyncio
async def test_persisted_workflow_restores_fences_and_strips_decision_input() -> None:
    legacy = AsyncMock(return_value={"status": "handled"})
    dependencies = _dependencies(commands_enabled=True, legacy=legacy)
    runner = SimpleNamespace(
        command={
            "job_id": JOB_ID,
            "payload": {
                "should_stop": True,
                "goal_achieved": True,
                "error": None,
                "freeze_data": None,
                "_accepted_completion_decision": {"private": True},
            },
            "accepted_lease_token": 7,
            "accepted_agent_id": str(AGENT_ID),
            "client_report_id": str(REPORT_ID),
        }
    )

    result = await job_completion.run_persisted_completion_workflow(
        runner,
        dependencies=job_completion.PersistedCompletionDependencies(
            legacy_complete=dependencies.legacy_complete
        ),
    )

    assert result == {"status": "handled"}
    called_body = legacy.await_args.args[2]
    assert called_body.lease_token == 7
    assert called_body.agent_id == AGENT_ID
    assert called_body.client_report_id == REPORT_ID
    assert not hasattr(called_body, "_accepted_completion_decision")
