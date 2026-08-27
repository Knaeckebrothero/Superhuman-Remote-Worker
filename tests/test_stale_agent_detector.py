"""Unit tests for stale-agent background sweeps."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import orchestrator.main as main
from orchestrator.database.postgres import (
    LeaseRecoveryBatch,
    LeaseRecoveryCircuitTrip,
    OrphanRecoveryBatch,
    RecoveredJob,
)


def _mock_db(shutdown_event: asyncio.Event, stall_return: int = 0):
    """Build a db mock matching the call surface of stale_agent_detector."""
    db = AsyncMock()

    def _stop(event):
        # Ensure the loop exits immediately after one full sweep.
        event.set()
        return []

    db.mark_stale_agents_offline = AsyncMock(
        side_effect=lambda *args, **kwargs: _stop(shutdown_event)
    )
    db.mark_stuck_working_agents_ready = AsyncMock(return_value=0)
    db.mark_stalled_working_agents_by_graph_progress = AsyncMock(
        return_value=stall_return
    )
    db.mark_stuck_session_agents_ready = AsyncMock(return_value=0)
    db.reap_orphaned_session_agents = AsyncMock(return_value=[])
    db.list_retryable_thread_attach_abort_successors = AsyncMock(return_value=[])
    db.mark_orphaned_threads_ended = AsyncMock(return_value=[])
    db.mark_orphaned_threads_suspended = AsyncMock(return_value=[])
    db.abort_stale_pinned_retirement_preflights = AsyncMock(return_value=[])
    db.list_retryable_pinned_retirements = AsyncMock(return_value=[])
    db.recover_orphaned_jobs = AsyncMock(return_value=OrphanRecoveryBatch())
    db.recover_expired_lease_jobs = AsyncMock(return_value=LeaseRecoveryBatch())
    db.gc_offline_agents = AsyncMock(return_value=0)
    return db


@pytest.mark.asyncio
async def test_detector_retries_durable_attach_abort_after_request_task_failure():
    """The leader/startup sweep owns G2 even if the request-local task dies."""

    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    candidate = {
        "thread_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
        "retired_runtime_generation": "11111111-1111-4111-8111-111111111111",
        "retired_attach_token": "22222222-2222-4222-8222-222222222222",
        "retired_agent_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2",
        "successor_generation": "55555555-5555-4555-8555-555555555555",
        "quiescence_protocol": "agent_attach_not_started_v1",
        "workspace_generation": None,
        "workspace_runtime_incarnation": None,
    }
    db.list_retryable_thread_attach_abort_successors = AsyncMock(
        return_value=[candidate]
    )
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=candidate)

    @asynccontextmanager
    async def acquire():
        yield conn

    db.acquire = MagicMock(side_effect=acquire)
    reconcile = AsyncMock(side_effect=[RuntimeError("transient"), True])
    main._attach_abort_successor_tasks.clear()
    original_schedule = main._schedule_attach_abort_successor

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_reconcile_attach_abort_successor", reconcile),
    ):
        first = original_schedule(
            candidate["thread_id"],
            retired_runtime_generation=candidate["retired_runtime_generation"],
            retired_attach_token=candidate["retired_attach_token"],
            retired_agent_id=candidate["retired_agent_id"],
        )
        await first
        assert not main._attach_abort_successor_tasks

        scheduled = []

        def schedule_from_sweep(*args, **kwargs):
            task = original_schedule(*args, **kwargs)
            scheduled.append(task)
            return task

        with (
            patch.object(main, "_schedule_attach_abort_successor", schedule_from_sweep),
            patch.object(main, "_trigger_dispatch", MagicMock()),
            patch.object(main, "_release_thread_resources", AsyncMock()),
            patch.object(main, "_suspend_thread_resources", AsyncMock()),
        ):
            await main.stale_agent_detector(shutdown_event)
        assert len(scheduled) == 1
        await scheduled[0]

    assert reconcile.await_count == 2
    db.list_retryable_thread_attach_abort_successors.assert_awaited_once_with(limit=25)
    # The same detector pass continues through unrelated recovery work.
    db.recover_orphaned_jobs.assert_awaited_once()
    db.gc_offline_agents.assert_awaited_once_with(retention_hours=24)


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
    agents-table sweeps (knowledge-base/knowledge/features/job_execution_lease.md).
    The wake goes to the owning project's officer only — never the fleet."""
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    db.recover_expired_lease_jobs = AsyncMock(
        return_value=LeaseRecoveryBatch(
            recovered_jobs=(
                RecoveredJob(job_id="job-a", project_id="proj-1"),
                RecoveredJob(job_id="job-b", project_id="proj-1"),
            )
        )
    )

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch") as trigger_dispatch,
        patch.object(main, "_kick_officer_event_drain") as kick_wake_drain,
        patch.object(main, "notify_all_officers", AsyncMock()) as notify_all,
        patch.object(main, "notify_owning_officers", AsyncMock()) as notify_owning,
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    db.recover_expired_lease_jobs.assert_awaited_once_with(
        completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED
    )
    notify_owning.assert_awaited_once_with(
        db,
        {"proj-1": {"summary": "2 job(s) recovered by lease expiry: job-a, job-b"}},
        source="fleet",
        dedup_key="fleet:lease_recovered",
    )
    notify_all.assert_not_awaited()
    kick_wake_drain.assert_called_once_with(db)
    trigger_dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_lease_recovery_groups_wakes_per_owning_project():
    """A batch spanning projects sends each officer only its own jobs' ids;
    a job with no project notifies nobody (owner ruling: job-derived fleet
    events are scoped, not broadcast)."""
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    db.recover_expired_lease_jobs = AsyncMock(
        return_value=LeaseRecoveryBatch(
            recovered_jobs=(
                RecoveredJob(job_id="job-a", project_id="proj-1"),
                RecoveredJob(job_id="job-b", project_id="proj-2"),
                RecoveredJob(job_id="job-c", project_id=None),
            )
        )
    )

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch"),
        patch.object(main, "_kick_officer_event_drain"),
        patch.object(main, "notify_all_officers", AsyncMock()) as notify_all,
        patch.object(main, "notify_owning_officers", AsyncMock()) as notify_owning,
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    notify_owning.assert_awaited_once_with(
        db,
        {
            "proj-1": {"summary": "1 job(s) recovered by lease expiry: job-a"},
            "proj-2": {"summary": "1 job(s) recovered by lease expiry: job-b"},
        },
        source="fleet",
        dedup_key="fleet:lease_recovered",
    )
    notify_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_lease_recovery_of_projectless_jobs_notifies_nobody():
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    db.recover_expired_lease_jobs = AsyncMock(
        return_value=LeaseRecoveryBatch(
            recovered_jobs=(RecoveredJob(job_id="job-a", project_id=None),)
        )
    )

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch") as trigger_dispatch,
        patch.object(main, "_kick_officer_event_drain") as kick_wake_drain,
        patch.object(main, "notify_all_officers", AsyncMock()) as notify_all,
        patch.object(main, "notify_owning_officers", AsyncMock()) as notify_owning,
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    notify_owning.assert_not_awaited()
    notify_all.assert_not_awaited()
    kick_wake_drain.assert_not_called()
    # The job still goes back to the dispatcher — scoping affects wakes only.
    trigger_dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_orphan_recovery_wakes_only_the_owning_projects_officer():
    """fleet:orphans_recovered is scoped: the owning project's officer hears
    about its own jobs; projectless jobs notify nobody; the fleet fan-out is
    never used for job-derived events."""
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)
    db.recover_orphaned_jobs = AsyncMock(
        return_value=OrphanRecoveryBatch(
            count=3,
            recovered_jobs=(
                RecoveredJob(job_id="aaaa1111-dead-beef", project_id="proj-a"),
                RecoveredJob(job_id="bbbb2222-dead-beef", project_id="proj-a"),
                RecoveredJob(job_id="cccc3333-dead-beef", project_id=None),
            ),
        )
    )

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch") as trigger_dispatch,
        patch.object(main, "_kick_officer_event_drain"),
        patch.object(main, "notify_all_officers", AsyncMock()) as notify_all,
        patch.object(main, "notify_owning_officers", AsyncMock()) as notify_owning,
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    notify_owning.assert_awaited_once_with(
        db,
        {
            "proj-a": {
                "summary": (
                    "2 orphaned job(s) auto-paused for re-dispatch "
                    "(agent offline): aaaa1111, bbbb2222"
                )
            }
        },
        source="fleet",
        dedup_key="fleet:orphans_recovered",
    )
    notify_all.assert_not_awaited()
    trigger_dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_agents_offline_scopes_to_derived_projects_and_falls_back_global():
    """Dead agents whose project is derivable (from their assigned/last job)
    wake that project's officer; only the underivable remainder keeps the
    historical fleet-wide fan-out."""
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)

    def _mark(*args, **kwargs):
        shutdown_event.set()
        return [
            {"agent_id": "agent-1", "project_id": "proj-a"},
            {"agent_id": "agent-2", "project_id": "proj-a"},
            {"agent_id": "agent-3", "project_id": None},
        ]

    db.mark_stale_agents_offline = AsyncMock(side_effect=_mark)

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch"),
        patch.object(main, "_kick_officer_event_drain") as kick_wake_drain,
        patch.object(main, "notify_all_officers", AsyncMock()) as notify_all,
        patch.object(main, "notify_owning_officers", AsyncMock()) as notify_owning,
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    notify_owning.assert_awaited_once_with(
        db,
        {"proj-a": {"summary": "2 agent(s) marked offline (missed heartbeats)"}},
        source="fleet",
        dedup_key="fleet:agents_offline",
    )
    notify_all.assert_awaited_once_with(
        db,
        source="fleet",
        dedup_key="fleet:agents_offline",
        payload={"summary": "1 agent(s) marked offline (missed heartbeats)"},
    )
    kick_wake_drain.assert_called_once_with(db)


@pytest.mark.asyncio
async def test_agents_offline_fully_derivable_skips_the_fleet_fanout():
    shutdown_event = asyncio.Event()
    db = _mock_db(shutdown_event)

    def _mark(*args, **kwargs):
        shutdown_event.set()
        return [{"agent_id": "agent-1", "project_id": "proj-a"}]

    db.mark_stale_agents_offline = AsyncMock(side_effect=_mark)

    with (
        patch.object(main, "postgres_db", db),
        patch.object(main, "_trigger_dispatch"),
        patch.object(main, "_kick_officer_event_drain"),
        patch.object(main, "notify_all_officers", AsyncMock()) as notify_all,
        patch.object(main, "notify_owning_officers", AsyncMock()) as notify_owning,
        patch.object(main, "_release_thread_resources", AsyncMock()),
        patch.object(main, "_suspend_thread_resources", AsyncMock()),
    ):
        await main.stale_agent_detector(shutdown_event)

    notify_owning.assert_awaited_once()
    notify_all.assert_not_awaited()


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
