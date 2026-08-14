"""Focused unit contracts for the durable completion-sweep router."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from orchestrator.services.completion_sweep_router import (
    ACTION_RESULT_LIMIT_BYTES,
    STATELESS_OWNER_GAP_MESSAGE,
    CompletionSweepRouter,
    _ClaimedAction,
    _bounded_action_result,
)


JOB_ID = "11111111-aaaa-4111-8111-111111111111"
COMMAND_ID = "22222222-bbbb-4222-8222-222222222222"


class _ClaimHarness(CompletionSweepRouter):
    def __init__(self, *, route: str, finalizer, alert=None) -> None:
        super().__init__(object(), finalizer, alert, claimant_id="unit-router")
        self.action = _ClaimedAction(
            job_id=JOB_ID,
            attempt=3,
            command_id=COMMAND_ID,
            command_attempt=2,
            route=route,
            claimed_by="unit-router:33333333-cccc-4333-8333-333333333333",
        )
        self.sources: list[str] = []
        self.completed: list[dict] = []

    async def _allocate_and_claim(self, job_id: str, *, source: str):
        assert job_id == JOB_ID
        self.sources.append(source)
        return None, self.action

    async def _complete(self, action, route, result):
        assert action is self.action
        assert route == result["route"]
        self.completed.append(dict(result))
        return True


class _LostHeartbeatHarness(_ClaimHarness):
    def __init__(self, *, finalizer) -> None:
        super().__init__(route="resume_finalizer", finalizer=finalizer)
        self.action_lease_seconds = 0.03
        self.action_heartbeat_seconds = 0.01

    async def _renew(self, action):
        assert action is self.action
        return False


@pytest.mark.parametrize(
    ("route", "finalizer_calls", "alert_calls"),
    [
        ("resume_finalizer", 1, 0),
        ("park_alert", 1, 1),
        ("alert_only", 0, 1),
    ],
)
@pytest.mark.asyncio
async def test_actionable_routes_call_only_the_authorized_collaborators(
    route: str, finalizer_calls: int, alert_calls: int
) -> None:
    finalizer = SimpleNamespace(
        finalize_command=AsyncMock(
            return_value=SimpleNamespace(
                command_id=COMMAND_ID,
                state="done",
                disposition="done",
                outcome={"large": "x" * (ACTION_RESULT_LIMIT_BYTES * 2)},
                error_code=None,
            )
        )
    )
    alert = AsyncMock()
    router = _ClaimHarness(route=route, finalizer=finalizer, alert=alert)

    routed = await router.route_job(UUID(JOB_ID), source="  stale-agent  ")

    assert routed.disposition == "completed"
    assert routed.route == route
    assert routed.action_attempt == 3
    assert router.sources == ["stale-agent"]
    assert len(router.completed) == 1
    assert "large" not in str(router.completed[0])
    assert finalizer.finalize_command.await_count == finalizer_calls
    if finalizer_calls:
        finalizer.finalize_command.assert_awaited_once_with(COMMAND_ID, inline=False)
    assert alert.await_count == alert_calls


@pytest.mark.asyncio
async def test_resume_promotes_to_park_alert_when_finalizer_observes_deadline_race() -> (
    None
):
    finalizer = SimpleNamespace(
        finalize_command=AsyncMock(
            return_value=SimpleNamespace(
                command_id=COMMAND_ID,
                state="parked",
                disposition="busy",
                error_code="deadline_or_attempts_exhausted",
            )
        )
    )
    alert = AsyncMock()
    router = _ClaimHarness(route="resume_finalizer", finalizer=finalizer, alert=alert)

    routed = await router.route_job(JOB_ID, source="expired-lease")

    assert routed.disposition == "completed"
    assert routed.route == "park_alert"
    assert router.completed == [
        {
            "route": "park_alert",
            "finalizer": {
                "command_id": COMMAND_ID,
                "state": "parked",
                "disposition": "busy",
                "error_code": "deadline_or_attempts_exhausted",
            },
            "alerted": True,
        }
    ]
    alert.assert_awaited_once()


@pytest.mark.asyncio
async def test_execution_error_leaves_claim_uncompleted_for_lease_takeover(
    caplog: pytest.LogCaptureFixture,
) -> None:
    finalizer = SimpleNamespace(
        finalize_command=AsyncMock(side_effect=RuntimeError("database unavailable"))
    )
    router = _ClaimHarness(route="resume_finalizer", finalizer=finalizer)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await router.route_job(JOB_ID, source="orphan")

    assert router.completed == []
    assert "leaving claim for takeover" in caplog.text


@pytest.mark.asyncio
async def test_cancellation_leaves_claim_uncompleted_for_lease_takeover(
    caplog: pytest.LogCaptureFixture,
) -> None:
    finalizer = SimpleNamespace(
        finalize_command=AsyncMock(side_effect=asyncio.CancelledError)
    )
    router = _ClaimHarness(route="resume_finalizer", finalizer=finalizer)

    with pytest.raises(asyncio.CancelledError):
        await router.route_job(JOB_ID, source="expired-lease")

    assert router.completed == []
    assert "leaving claim for takeover" in caplog.text


@pytest.mark.asyncio
async def test_heartbeat_loss_fences_result_without_cancelling_finalizer() -> None:
    finished = asyncio.Event()

    async def finalize(command_id: str, *, inline: bool):
        assert command_id == COMMAND_ID
        assert not inline
        await asyncio.sleep(0.03)
        finished.set()
        return SimpleNamespace(state="done", disposition="done")

    router = _LostHeartbeatHarness(finalizer=SimpleNamespace(finalize_command=finalize))
    routed = await router.route_job(JOB_ID, source="orphan")

    assert finished.is_set()
    assert routed.disposition == "claim_lost"
    assert router.completed == []


def test_inputs_and_durable_result_are_bounded() -> None:
    finalizer = SimpleNamespace(finalize_command=AsyncMock())
    with pytest.raises(ValueError, match="action_lease_seconds"):
        CompletionSweepRouter(object(), finalizer, action_lease_seconds=0)
    with pytest.raises(ValueError, match="poll_seconds"):
        CompletionSweepRouter(object(), finalizer, poll_seconds=0)
    with pytest.raises(ValueError, match="claimant_id"):
        CompletionSweepRouter(object(), finalizer, claimant_id="  ")
    with pytest.raises(ValueError, match="8 KiB"):
        _bounded_action_result({"detail": "x" * ACTION_RESULT_LIMIT_BYTES})
    assert 0 < len(STATELESS_OWNER_GAP_MESSAGE) <= 256


@pytest.mark.asyncio
async def test_route_job_rejects_empty_source_before_touching_database() -> None:
    router = CompletionSweepRouter(
        object(), SimpleNamespace(finalize_command=AsyncMock())
    )
    with pytest.raises(ValueError, match="source must be nonempty"):
        await router.route_job(JOB_ID, source="  ")


@pytest.mark.asyncio
async def test_run_recovers_tick_errors_and_stops_cooperatively() -> None:
    shutdown = asyncio.Event()
    router = CompletionSweepRouter(
        object(),
        SimpleNamespace(finalize_command=AsyncMock()),
        claimant_id="loop-router",
        poll_seconds=0.01,
    )
    calls = 0

    async def route_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient scan failure")
        shutdown.set()

    router.route_once = route_once

    await asyncio.wait_for(router.run(shutdown), timeout=1)

    assert calls == 2


@pytest.mark.asyncio
async def test_maintenance_runs_nonexecuting_safety_net() -> None:
    safety = SimpleNamespace(
        reconcile_batch=AsyncMock(return_value=SimpleNamespace(scanned=0, results=()))
    )
    router = CompletionSweepRouter(
        object(),
        SimpleNamespace(finalize_command=AsyncMock()),
        safety_net=safety,
    )
    router.park_stateless_owner_gaps_once = AsyncMock(return_value=())

    await router.maintenance_once()

    router.park_stateless_owner_gaps_once.assert_awaited_once_with(limit=50)
    safety.reconcile_batch.assert_awaited_once_with(limit=50)


@pytest.mark.asyncio
async def test_maintenance_failure_isolated_from_router_tick(caplog) -> None:
    safety = SimpleNamespace(
        reconcile_batch=AsyncMock(side_effect=RuntimeError("database blip"))
    )
    router = CompletionSweepRouter(
        object(),
        SimpleNamespace(finalize_command=AsyncMock()),
        safety_net=safety,
    )
    router.park_stateless_owner_gaps_once = AsyncMock(return_value=())

    await router.maintenance_once()

    assert "completion safety-net tick failed" in caplog.text


@pytest.mark.asyncio
async def test_owner_gap_failure_isolated_from_existing_safety_net(caplog) -> None:
    safety = SimpleNamespace(
        reconcile_batch=AsyncMock(return_value=SimpleNamespace(scanned=0, results=()))
    )
    router = CompletionSweepRouter(
        object(),
        SimpleNamespace(finalize_command=AsyncMock()),
        safety_net=safety,
    )
    router.park_stateless_owner_gaps_once = AsyncMock(
        side_effect=RuntimeError("owner-gap scan unavailable")
    )

    await router.maintenance_once()

    assert "stateless owner-gap rescue tick failed" in caplog.text
    safety.reconcile_batch.assert_awaited_once_with(limit=50)
