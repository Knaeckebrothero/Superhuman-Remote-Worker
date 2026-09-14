"""Characterization for B08's explicit subjob and recovery boundaries."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.completion import LLM_OUTAGE_CEILING_SECONDS
from orchestrator.services.completion_recovery import (
    CompletionRecoveryDependencies,
    check_delegation_timeouts,
    infra_transient_sweep_once,
    llm_outage_sweep_once,
)
from orchestrator.services.subjob_completion import (
    DelegationCompletionDependencies,
    ScholarCompletionDependencies,
    escalate_target,
    handle_delegation_child_completion,
    handle_scholar_completion,
    spawn_scholar_subjob,
)
from orchestrator.services.subjob_output import (
    SubjobOutputDependencies,
    graft_subjob_output,
)


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


def _scholar_dependencies(store=None):
    store = store or MagicMock()
    return ScholarCompletionDependencies(
        store=store,
        forge=MagicMock(),
        trigger_dispatch=MagicMock(),
        resolve_workspace_backend=lambda _job: "sandbox",
        is_lite_config_override=lambda _override: False,
        should_provision_parent_container=lambda _override: False,
        revalidate_datasource_selection=AsyncMock(return_value=([], {})),
        datasource_selection_provenance=AsyncMock(return_value={}),
        prepare_primary_repository_authority=AsyncMock(return_value=None),
        completion_resume_guard_kwargs=lambda: {"completion_guard": True},
        maybe_wake_session=AsyncMock(),
        kick_session_wake_drain=MagicMock(),
        notify_review_returned=AsyncMock(),
    )


def _recovery_dependencies(store=None):
    store = store or MagicMock()
    return CompletionRecoveryDependencies(
        store=store,
        completion_commands_enabled=lambda: True,
        trigger_dispatch=MagicMock(),
        completion_resume_guard_kwargs=lambda: {"resume_guard": True},
        completion_dispatch_guard_kwargs=lambda: {"dispatch_guard": True},
        wait_for_stateless_cancel_settle=AsyncMock(return_value=True),
        notify_operator_freeze=AsyncMock(),
        handle_scholar_completion=AsyncMock(),
        handle_delegation_child_completion=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_command_graft_reconciles_external_commit_before_writing(monkeypatch):
    store = MagicMock()
    store.get_job = AsyncMock(
        side_effect=[
            {
                "id": "child-1",
                "parent_job_id": "parent-1",
                "repo_name": "job-parent",
                "branch_name": "subjob/child",
                "context": {},
            },
            {"id": "parent-1", "branch_name": "main"},
        ]
    )
    store.update_job_merge_status = AsyncMock(return_value=True)
    store.merge_job_context = AsyncMock(return_value=True)
    forge = MagicMock()
    forge.is_initialized = True
    forge.list_tree = AsyncMock()
    forge.change_files = AsyncMock()
    prior = MagicMock(output_path="outputs/001-scholar-child", commit_sha="abc")
    probe = AsyncMock(return_value=prior)
    monkeypatch.setattr("orchestrator.services.subjob_output.probe_graft_commit", probe)

    result = await graft_subjob_output(
        "child-1",
        completion_command_id="command-1",
        dependencies=SubjobOutputDependencies(store=store, forge=forge),
    )

    assert result == {
        "status": "grafted",
        "reason": "reconciled-command-trailer",
        "base_branch": "main",
        "output_path": "outputs/001-scholar-child",
        "commit_sha": "abc",
    }
    forge.list_tree.assert_not_awaited()
    forge.change_files.assert_not_awaited()
    store.update_job_merge_status.assert_awaited_once_with(
        "child-1", merge_status="grafted"
    )


@pytest.mark.asyncio
async def test_nonterminal_scholar_keeps_parent_waiting():
    store = MagicMock()
    store.get_job = AsyncMock()
    dependencies = _scholar_dependencies(store)

    await handle_scholar_completion(
        {
            "id": "scholar-1",
            "parent_job_id": "parent-1",
            "status": "paused",
            "context": {"scholar_target": "parent-1"},
        },
        [],
        dependencies=dependencies,
    )

    store.get_job.assert_not_awaited()
    dependencies.trigger_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_scholar_materialization_failure_releases_parent_hold(monkeypatch):
    store = MagicMock()
    store.get_user = AsyncMock(return_value={"id": "user-1"})
    store.update_job_status = AsyncMock()
    store.merge_job_context = AsyncMock()
    store.create_job = AsyncMock(side_effect=RuntimeError("authority changed"))
    dependencies = _scholar_dependencies(store)
    monkeypatch.setattr(
        "orchestrator.services.completion.resolve_scholar_config_from_disk",
        lambda *_args, **_kwargs: {"enabled": True, "scholar_config": "scholar"},
    )
    monkeypatch.setattr(
        "orchestrator.services.completion.format_scholar_instructions",
        lambda **_kwargs: "instructions",
    )

    with pytest.raises(RuntimeError, match="authority changed"):
        await spawn_scholar_subjob(
            {
                "id": "parent-1",
                "user_id": "user-1",
                "description": "research this",
                "context": {},
            },
            "worker",
            {},
            {},
            dependencies=dependencies,
        )

    assert [
        call.kwargs["status"] for call in store.update_job_status.await_args_list
    ] == [
        "waiting",
        "created",
    ]
    store.merge_job_context.assert_awaited_once_with(
        "parent-1", {"scholar_failed": True}
    )
    dependencies.trigger_dispatch.assert_called_once_with()


@pytest.mark.asyncio
async def test_target_escalation_writes_before_best_effort_wake_and_notification():
    store = MagicMock()
    store.update_job_status = AsyncMock()
    dependencies = _scholar_dependencies(store)

    assert (
        await escalate_target(
            "target-1",
            {"id": "target-1", "context": {}, "user_id": "user-1"},
            "critic could not verify",
            dependencies=dependencies,
        )
        == "pending_review"
    )
    store.update_job_status.assert_awaited_once_with(
        "target-1",
        status="pending_review",
        error_message="critic could not verify",
    )
    dependencies.maybe_wake_session.assert_awaited_once_with(
        "target-1", "pending_review"
    )
    dependencies.notify_review_returned.assert_awaited_once_with(
        user_id="user-1",
        job_id="target-1",
        config_name="",
        reason="critic could not verify",
    )


@pytest.mark.asyncio
async def test_completed_scholar_requeues_stateless_parent_with_fence():
    store = MagicMock()
    store.get_job = AsyncMock(
        side_effect=[
            {
                "id": "parent-1",
                "status": "waiting",
                "execution_lane": "stateless",
                "priority": 7,
                "user_id": "user-1",
            },
            {
                "id": "scholar-1",
                "context": {"graft_output_path": "outputs/001-scholar"},
            },
        ]
    )
    store.queue_stateless_job_for_resume = AsyncMock(return_value=True)
    dependencies = _scholar_dependencies(store)
    actions = []

    await handle_scholar_completion(
        {
            "id": "scholar-1",
            "parent_job_id": "parent-1",
            "status": "completed",
            "context": {"scholar_target": "parent-1"},
        },
        actions,
        dependencies=dependencies,
    )

    store.queue_stateless_job_for_resume.assert_awaited_once_with(
        "parent-1",
        {
            "scholar_completed": True,
            "scholar_output_dir": "outputs/001-scholar",
        },
        priority=7,
        fair_key="user-1",
        expected_status="waiting",
        completion_guard=True,
    )
    dependencies.trigger_dispatch.assert_called_once_with()
    assert actions == ["scholar scholar-1 completed, parent parent-1 unblocked"]


@pytest.mark.asyncio
async def test_compatibility_delegation_requeues_parent_once():
    store = MagicMock()
    store.all_delegation_children_terminal = AsyncMock(return_value=True)
    store.get_job = AsyncMock(
        return_value={
            "id": "parent-1",
            "status": "waiting",
            "execution_lane": "stateless",
            "priority": 4,
            "user_id": None,
        }
    )
    store.get_delegation_children = AsyncMock(
        return_value=[
            {
                "id": "child-1",
                "status": "completed",
                "config_name": "worker_base",
                "creation_order": 0,
                "context": {"graft_output_path": "outputs/001-worker"},
                "freeze_data": {"summary": "done", "confidence": 0.9},
            }
        ]
    )
    store.queue_stateless_job_for_resume = AsyncMock(return_value=True)
    trigger = MagicMock()
    dependencies = DelegationCompletionDependencies(
        store=store,
        trigger_dispatch=trigger,
        completion_resume_guard_kwargs=lambda: {"resume_guard": True},
    )
    actions = []

    await handle_delegation_child_completion(
        {"id": "child-1", "parent_job_id": "parent-1", "creation_order": 0},
        actions,
        dependencies=dependencies,
    )

    queued = store.queue_stateless_job_for_resume.await_args
    assert queued.args[0] == "parent-1"
    assert queued.args[1]["delegation_results"][0]["output_path"] == (
        "outputs/001-worker"
    )
    assert queued.kwargs["resume_guard"] is True
    trigger.assert_not_called()
    assert actions == [
        "delegation: all 1 children done, parent parent-1 re-queued (1 completed)"
    ]


@pytest.mark.asyncio
async def test_timeout_refuses_parent_resume_until_stateless_child_settles():
    now = datetime.now(timezone.utc)
    row = {
        "id": "parent-1",
        "freeze_data": {
            "freeze_type": "delegation",
            "timestamp": (now - timedelta(hours=3)).isoformat(),
            "timeout": 7200,
        },
        "execution_lane": "pinned",
        "priority": 0,
        "user_id": None,
    }
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=[row])
    store = MagicMock()
    store.acquire = lambda: _Acquire(conn)
    store.get_delegation_children = AsyncMock(
        return_value=[
            {
                "id": "child-1",
                "status": "processing",
                "execution_lane": "stateless",
                "context": {},
            }
        ]
    )
    store.cancel_stateless_job = AsyncMock(return_value=(True, True))
    store.claim_delegation_resume = AsyncMock(return_value=True)
    dependencies = _recovery_dependencies(store)
    object.__setattr__(
        dependencies,
        "wait_for_stateless_cancel_settle",
        AsyncMock(return_value=False),
    )

    assert await check_delegation_timeouts(dependencies=dependencies) == 0
    store.cancel_stateless_job.assert_awaited_once_with("child-1", dispatch_guard=True)
    store.claim_delegation_resume.assert_not_awaited()


def _due_outage_job(*, child: bool) -> dict:
    now = datetime.now(timezone.utc)
    job = {
        "id": "job-1",
        "freeze_data": {
            "freeze_type": "llm_unavailable",
            "next_retry_at": now.isoformat(),
            "attempt": 3,
        },
        "context": {
            "llm_outage": {
                "attempt": 3,
                "first_failed_at": (
                    now - timedelta(seconds=LLM_OUTAGE_CEILING_SECONDS + 60)
                ).isoformat(),
                "last_failed_at": now.isoformat(),
            }
        },
    }
    if child:
        job["parent_job_id"] = "parent-1"
    return job


@pytest.mark.asyncio
async def test_outage_ceiling_failure_runs_parent_continuation_handlers():
    store = MagicMock()
    store.list_due_llm_outage_jobs = AsyncMock(
        return_value=[_due_outage_job(child=True)]
    )
    store.fail_llm_outage_job = AsyncMock(return_value=True)
    store.claim_llm_outage_redispatch = AsyncMock()
    dependencies = _recovery_dependencies(store)

    assert await llm_outage_sweep_once(dependencies=dependencies) == (0, 1)
    dependencies.handle_scholar_completion.assert_awaited_once()
    dependencies.handle_delegation_child_completion.assert_awaited_once()
    assert (
        dependencies.handle_scholar_completion.await_args.args[0]["status"] == "failed"
    )
    dependencies.trigger_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_transient_recovery_uses_command_storage_fence_and_dispatches_once():
    store = MagicMock()
    store.list_due_backoff_jobs = AsyncMock(
        return_value=[{"id": "job-1"}, {"id": "job-2"}]
    )
    store.claim_backoff_redispatch = AsyncMock(side_effect=[True, False])
    dependencies = _recovery_dependencies(store)

    assert await infra_transient_sweep_once(dependencies=dependencies) == (2, 1)
    store.list_due_backoff_jobs.assert_awaited_once_with(
        "infra_transient", limit=50, completion_commands_enabled=True
    )
    assert store.claim_backoff_redispatch.await_count == 2
    dependencies.trigger_dispatch.assert_called_once_with()
