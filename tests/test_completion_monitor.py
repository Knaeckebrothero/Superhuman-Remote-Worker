"""Focused pure tests for completion liveness alarm policy."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from orchestrator.services.completion_monitor import (
    OLDEST_QUEUED_WORKER_BATCH_DEDUP_KEY,
    OLDEST_UNFINALIZED_COMMAND_DEDUP_KEY,
    ZERO_FINALIZER_LEADER_DEDUP_KEY,
    CompletionMonitor,
    CompletionMonitorSample,
)


NOW = datetime(2026, 8, 13, tzinfo=UTC)
COMMAND_ID = "22222222-bbbb-4222-8222-222222222222"
JOB_ID = "11111111-aaaa-4111-8111-111111111111"


def _sample(*, leaders: int = 1, age: float | None = None):
    return CompletionMonitorSample(
        observed_at=NOW,
        live_finalizer_leaders=leaders,
        oldest_command_id=COMMAND_ID if age is not None else None,
        oldest_job_id=JOB_ID if age is not None else None,
        oldest_state="parked" if age is not None else None,
        oldest_reported_at=NOW if age is not None else None,
        oldest_age_seconds=age,
    )


def test_startup_grace_suppresses_both_alarm_classes() -> None:
    ticks = [10.0]
    monitor = CompletionMonitor(
        object(),
        lambda _alert: None,
        startup_grace_seconds=30,
        clock=lambda: ticks[0],
    )
    ticks[0] = 39.9
    assert monitor.alerts_for(_sample(leaders=0, age=9_999)) == ()


def test_fixed_dedup_keys_and_parked_age_alarm() -> None:
    ticks = [10.0]
    monitor = CompletionMonitor(
        object(),
        lambda _alert: None,
        max_unfinalized_age_seconds=1_800,
        startup_grace_seconds=30,
        clock=lambda: ticks[0],
    )
    ticks[0] = 40.0

    alerts = monitor.alerts_for(_sample(leaders=0, age=1_800))

    assert [alert.dedup_key for alert in alerts] == [
        ZERO_FINALIZER_LEADER_DEDUP_KEY,
        OLDEST_UNFINALIZED_COMMAND_DEDUP_KEY,
    ]
    assert alerts[1].command_state == "parked"
    assert alerts[1].age_seconds == 1_800


def test_runnable_worker_age_uses_fixed_key_independently_of_commands() -> None:
    ticks = [10.0]
    monitor = CompletionMonitor(
        object(),
        lambda _alert: None,
        completion_commands_enabled=False,
        max_queued_worker_age_seconds=300,
        startup_grace_seconds=30,
        clock=lambda: ticks[0],
    )
    ticks[0] = 40.0
    sample = _sample(leaders=0, age=9_999)
    sample = replace(
        sample,
        oldest_worker_unit_id=JOB_ID,
        oldest_worker_state="queued",
        oldest_worker_runnable_at=NOW,
        oldest_worker_age_seconds=300,
    )

    assert monitor.alerts_for(replace(sample, oldest_worker_age_seconds=299.999)) == ()
    alerts = monitor.alerts_for(sample)

    assert [alert.dedup_key for alert in alerts] == [
        OLDEST_QUEUED_WORKER_BATCH_DEDUP_KEY
    ]
    assert alerts[0].unit_id == JOB_ID
    assert alerts[0].queue_state == "queued"
    assert alerts[0].runnable_at == NOW
    assert alerts[0].age_seconds == 300


@pytest.mark.asyncio
async def test_commands_off_sample_queries_only_run_queue() -> None:
    queries: list[str] = []

    class _Conn:
        async def fetchrow(self, query, *_args):
            queries.append(query)
            return {
                "observed_at": NOW,
                "oldest_worker_unit_id": None,
                "oldest_worker_state": None,
                "oldest_worker_runnable_at": None,
                "oldest_worker_age_seconds": None,
            }

    sample = await CompletionMonitor(
        _Conn(),
        lambda _alert: None,
        completion_commands_enabled=False,
    ).sample()

    assert sample.live_finalizer_leaders == 0
    assert sample.oldest_command_id is None
    assert len(queries) == 1
    assert "run_queue" in queries[0]
    assert "job_completion_" not in queries[0]
    assert "completion_finalizer_" not in queries[0]


@pytest.mark.asyncio
async def test_run_once_supports_async_sink() -> None:
    sink = AsyncMock()
    monitor = CompletionMonitor(
        object(), sink, startup_grace_seconds=0, clock=lambda: 1.0
    )
    monitor.sample = AsyncMock(return_value=_sample(leaders=0))

    alerts = await monitor.run_once()

    assert len(alerts) == 1
    sink.assert_awaited_once_with(alerts[0])


@pytest.mark.asyncio
async def test_monitor_loop_recovers_from_tick_error_and_stops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = asyncio.Event()
    monitor = CompletionMonitor(object(), lambda _alert: None, poll_seconds=0.01)
    calls = 0

    async def tick():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")
        stop.set()
        return ()

    monitor.run_once = tick
    await asyncio.wait_for(monitor.run(stop), timeout=1)

    assert calls == 2
    assert "completion monitor tick failed" in caplog.text


def test_monitor_configuration_is_bounded() -> None:
    with pytest.raises(ValueError, match="alert"):
        CompletionMonitor(object(), None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_unfinalized"):
        CompletionMonitor(object(), lambda _: None, max_unfinalized_age_seconds=0)
    with pytest.raises(ValueError, match="max_queued_worker"):
        CompletionMonitor(object(), lambda _: None, max_queued_worker_age_seconds=0)
    with pytest.raises(ValueError, match="startup_grace"):
        CompletionMonitor(object(), lambda _: None, startup_grace_seconds=-1)
    with pytest.raises(ValueError, match="poll_seconds"):
        CompletionMonitor(object(), lambda _: None, poll_seconds=0)
