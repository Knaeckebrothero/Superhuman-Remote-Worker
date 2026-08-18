"""Unit tests for stale-agent background sweeps."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import orchestrator.main as main
from orchestrator.database.postgres import (
    LeaseRecoveryBatch,
    LeaseRecoveryCircuitTrip,
)


def _mock_db(shutdown_event: asyncio.Event, stall_return: int = 0):
    """Build a db mock matching the call surface of stale_agent_detector."""
    db = AsyncMock()

    def _stop(event):
        # Ensure the loop exits immediately after one full sweep.
        event.set()
        return 0

    db.mark_stale_agents_offline = AsyncMock(
        side_effect=lambda *args, **kwargs: _stop(shutdown_event)
    )
    db.mark_stuck_working_agents_ready = AsyncMock(return_value=0)
    db.mark_stalled_working_agents_by_graph_progress = AsyncMock(
        return_value=stall_return
    )
    db.mark_stuck_session_agents_ready = AsyncMock(return_value=0)
    db.reap_orphaned_session_agents = AsyncMock(return_value=[])
    db.mark_orphaned_threads_ended = AsyncMock(return_value=[])
    db.mark_orphaned_threads_suspended = AsyncMock(return_value=[])
    db.recover_orphaned_jobs = AsyncMock(return_value=0)
    db.recover_expired_lease_jobs = AsyncMock(return_value=LeaseRecoveryBatch())
    db.gc_offline_agents = AsyncMock(return_value=0)
    return db


@pytest.mark.asyncio
async def test_stale_detector_uses_graph_progress_stall_window():
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event, stall_return=0)

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch", MagicMock()),
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    db.mark_stalled_working_agents_by_graph_progress.assert_awaited_once_with(
        stall_minutes=10
    )
    method_calls = [call[0] for call in db.method_calls]
    assert method_calls.index("mark_stale_agents_offline") < method_calls.index(
        "mark_stuck_working_agents_ready"
    )
    assert method_calls.index("mark_stuck_working_agents_ready") < method_calls.index(
        "mark_stalled_working_agents_by_graph_progress"
    )
    assert method_calls.index(
        "mark_stalled_working_agents_by_graph_progress"
    ) < method_calls.index("mark_stuck_session_agents_ready")
    assert method_calls.index("mark_stuck_session_agents_ready") < method_calls.index(
        "reap_orphaned_session_agents"
    )


@pytest.mark.asyncio
async def test_stale_detector_triggers_dispatch_on_graph_progress_stall():
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event, stall_return=3)

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch") as trigger_dispatch,
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    trigger_dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_step_failure_does_not_block_downstream_recovery():
    """One broken sweep must degrade only itself.

    Regression for the 2026-07-11 incident: a bind-type crash in the
    graph-progress sweep aborted the shared try block and silently disabled
    recover_orphaned_jobs (and every other downstream step) for ~36h. See
    knowledge-history/done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md.
    """
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    db.mark_stalled_working_agents_by_graph_progress = AsyncMock(
        side_effect=TypeError(
            "invalid input for query argument $1: 10 (expected str, got int)"
        )
    )

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch", MagicMock()),
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    # Everything downstream of the crashing step still ran.
    db.mark_stuck_session_agents_ready.assert_awaited_once()
    db.reap_orphaned_session_agents.assert_awaited_once()
    db.mark_orphaned_threads_ended.assert_awaited_once()
    db.mark_orphaned_threads_suspended.assert_awaited_once()
    db.recover_orphaned_jobs.assert_awaited_once_with(
        completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED
    )
    db.recover_expired_lease_jobs.assert_awaited_once_with(
        completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED
    )
    db.gc_offline_agents.assert_awaited_once()


@pytest.mark.asyncio
async def test_lease_expiry_recovery_runs_and_triggers_dispatch():
    """Expired-lease jobs are recovered and re-dispatched, independent of the
    agents-table sweeps (knowledge-base/knowledge/features/job_execution_lease.md)."""
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    db.recover_expired_lease_jobs = AsyncMock(
        return_value=LeaseRecoveryBatch(recovered_job_ids=("job-a", "job-b"))
    )

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch") as trigger_dispatch,
        patch.object(main, "_kick_officer_event_drain") as kick_wake_drain,
        patch.object(main, "notify_all_officers", AsyncMock()) as notify_all,
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    db.recover_expired_lease_jobs.assert_awaited_once_with(
        completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED
    )
    notify_all.assert_awaited_once_with(
        db,
        source="fleet",
        dedup_key="fleet:lease_recovered",
        payload={"summary": "2 job(s) recovered by lease expiry: job-a, job-b"},
    )
    kick_wake_drain.assert_called_once_with(db)
    trigger_dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_lease_recovery_uses_strict_audit_fingerprint_reader():
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    strict_counts = AsyncMock(return_value={})
    reader = MagicMock(is_available=True, get_audit_counts_strict=strict_counts)

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "audit_reader", reader),
        patch.object(main, "_trigger_dispatch", MagicMock()),
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    db.recover_expired_lease_jobs.assert_awaited_once_with(
        completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED,
        audit_fingerprint_provider=strict_counts,
    )


@pytest.mark.asyncio
async def test_lease_circuit_trip_kicks_only_durable_wake_drain_not_dispatch():
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    db.recover_expired_lease_jobs = AsyncMock(
        return_value=LeaseRecoveryBatch(
            circuit_trips=(
                LeaseRecoveryCircuitTrip(
                    job_id="job-a",
                    project_id="project-a",
                    unchanged_recoveries=3,
                    officer_destination="wake",
                    officer_thread_id="thread-a",
                    notification_queued=True,
                ),
            )
        )
    )

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch") as trigger_dispatch,
        patch.object(main, "_kick_officer_event_drain") as kick_wake_drain,
        patch.object(main, "notify_all_officers", AsyncMock()) as notify_all,
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    trigger_dispatch.assert_not_called()
    kick_wake_drain.assert_called_once_with(db)
    notify_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_recovery_survives_orphan_recovery_failure():
    """The lease sweep is the PRIMARY recovery path — a failure in the legacy
    agents-join sweep must not take it down (per-step isolation)."""
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    db.recover_orphaned_jobs = AsyncMock(side_effect=RuntimeError("boom"))

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch", MagicMock()),
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    db.recover_expired_lease_jobs.assert_awaited_once_with(
        completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED
    )
    db.gc_offline_agents.assert_awaited_once()
