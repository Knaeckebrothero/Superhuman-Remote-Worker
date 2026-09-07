"""Crash/replay contracts for the external project-loop handoff."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock
from uuid import uuid4

import pytest

import orchestrator.main
from orchestrator.services.completion_finalizer import (
    EFFECT_DETAIL_LIMIT_BYTES,
    _bounded_effect_detail,
)
from orchestrator.services.project_loop_atomic import (
    ProjectLoopHandoffAuthorityLost,
    plan_loop_advance,
)
from orchestrator.services.project_loop_sweeper import _sweep_tick


def _marker_job(output, *, state="pending", result=None, command_id=None):
    marker = {
        "state": state,
        "command_id": command_id,
        "output": output,
    }
    if result is not None:
        marker["result"] = result
    return {
        "id": str(uuid4()),
        "status": "completed",
        "context": {"_project_loop_advance_handoff": marker},
    }


def test_handoff_retry_error_output_bounds_multibyte_provider_diagnostic():
    output = orchestrator.main._project_loop_handoff_error_output(
        RuntimeError("界" * 20_000)
    )

    assert output["actions"] == []
    assert len(output["error"].encode("utf-8")) <= 1024
    detail = _bounded_effect_detail("project_loop_advance_handoff", output)
    assert len(detail.encode("utf-8")) <= EFFECT_DETAIL_LIMIT_BYTES


@pytest.mark.asyncio
async def test_done_predecessor_marker_replays_without_external_tail(monkeypatch):
    output = {"applicable": True, "won": True, "loop_id": str(uuid4())}
    stored = {"actions": ["already handed off"]}
    job = _marker_job(output, state="done", result=stored)
    db = AsyncMock()
    db.get_job.return_value = job
    tail = AsyncMock(side_effect=AssertionError("external tail must not replay"))
    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    monkeypatch.setattr(orchestrator.main, "_handoff_atomic_project_loop_advance", tail)

    assert (
        await orchestrator.main._execute_persisted_project_loop_handoff(job, output)
        == stored
    )
    tail.assert_not_awaited()
    db.finish_project_loop_handoff.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_marker_settles_only_after_full_tail(monkeypatch):
    output = {"applicable": True, "won": True, "loop_id": str(uuid4())}
    result = {"actions": ["full tail"]}
    job = _marker_job(output)
    db = AsyncMock()
    db.get_job.return_value = job
    db.claim_project_loop_handoff.return_value = True
    db.renew_project_loop_handoff.return_value = True
    db.finish_project_loop_handoff.return_value = result
    tail = AsyncMock(return_value=result)
    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    monkeypatch.setattr(orchestrator.main, "_handoff_atomic_project_loop_advance", tail)

    assert (
        await orchestrator.main._execute_persisted_project_loop_handoff(job, output)
        == result
    )
    tail.assert_awaited_once_with(job, output, authority_check=ANY)
    claim = db.claim_project_loop_handoff.await_args
    assert claim.args == (job["id"],)
    assert claim.kwargs["expected_output"] == output
    assert claim.kwargs["claimant_id"].startswith("project-loop-handoff:")
    assert (
        claim.kwargs["lease_seconds"]
        == orchestrator.main._PROJECT_LOOP_HANDOFF_LEASE_SECONDS
    )
    finish = db.finish_project_loop_handoff.await_args
    assert finish.args == (job["id"],)
    assert finish.kwargs == {
        "expected_output": output,
        "result": result,
        "claimant_id": claim.kwargs["claimant_id"],
    }


@pytest.mark.asyncio
async def test_two_commandless_sweepers_share_one_leased_external_tail(monkeypatch):
    """Overlapping leader terms cannot both enter an unacknowledged tail."""

    output = {"applicable": True, "won": True, "loop_id": str(uuid4())}
    result = {"actions": ["one tail"]}
    job = _marker_job(output)
    db = AsyncMock()
    db.get_job.return_value = job
    claims = 0

    async def claim_once(*_args, **_kwargs):
        nonlocal claims
        claims += 1
        return claims == 1

    db.claim_project_loop_handoff.side_effect = claim_once
    db.renew_project_loop_handoff.return_value = True
    db.finish_project_loop_handoff.return_value = result
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_tail(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return result

    tail = AsyncMock(side_effect=slow_tail)
    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    monkeypatch.setattr(orchestrator.main, "_handoff_atomic_project_loop_advance", tail)

    first = asyncio.create_task(
        orchestrator.main._execute_persisted_project_loop_handoff(job, output)
    )
    await entered.wait()
    with pytest.raises(RuntimeError, match="another live claimant"):
        await orchestrator.main._execute_persisted_project_loop_handoff(job, output)
    release.set()
    assert await first == result
    tail.assert_awaited_once_with(job, output, authority_check=ANY)


@pytest.mark.asyncio
async def test_lease_loss_after_first_consequence_stops_tail_and_keeps_caller_alive(
    monkeypatch,
):
    """A stale claimant stops at the first post-await fence, without cancelling
    the long-lived reconciler task that called it.
    """

    loop_id, member_id, successor_id = (str(uuid4()) for _ in range(3))
    output = {
        "applicable": True,
        "won": True,
        "loop_id": loop_id,
        "completed_member_id": member_id,
        "spawned_job_ids": [successor_id],
        "total_jobs_run": 2,
        "replay": {
            "record_member": {"failed": False, "last_error": None},
            "notify_user_questions": True,
            "kb_ttl_decrement": True,
            "close_ticket": None,
            "notifications": [
                {
                    "event_type": "loop_stopped",
                    "subject": "stopped",
                    "message": "stopped",
                }
            ],
            "officer": None,
            "pre_actions": [],
            "action": {"kind": "rotation", "stage": "developer"},
        },
    }
    origin = _marker_job(output)
    origin["id"] = member_id
    db = AsyncMock()
    db.get_job.return_value = origin
    db.get_project_loop.return_value = {
        "id": loop_id,
        "project_id": str(uuid4()),
        "owner_id": str(uuid4()),
    }
    db.claim_project_loop_handoff.return_value = True
    # Tail start, post-loop-read, then ownership loss immediately after the
    # first durable consequence (member outcome recording).
    db.renew_project_loop_handoff.side_effect = [True, True, False]
    first_consequence = AsyncMock()
    user_questions = AsyncMock()
    provision = AsyncMock()
    ttl = AsyncMock()
    notify = AsyncMock()
    dispatch = MagicMock()
    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    monkeypatch.setattr(orchestrator.main, "vector_db", MagicMock())
    monkeypatch.setattr(
        orchestrator.main, "_record_loop_job_outcome", first_consequence
    )
    monkeypatch.setattr(
        orchestrator.main, "_notify_loop_user_questions", user_questions
    )
    monkeypatch.setattr(orchestrator.main, "_decrement_project_loop_kb_ttl_once", ttl)
    monkeypatch.setattr(orchestrator.main, "_notify_loop_event", notify)
    monkeypatch.setattr(orchestrator.main, "_trigger_dispatch", dispatch)
    monkeypatch.setattr(
        "orchestrator.services.job_provisioning.provision_job_repo", provision
    )

    with pytest.raises(ProjectLoopHandoffAuthorityLost, match="lease was lost"):
        await orchestrator.main._execute_persisted_project_loop_handoff(origin, output)

    first_consequence.assert_awaited_once()
    user_questions.assert_not_awaited()
    provision.assert_not_awaited()
    ttl.assert_not_awaited()
    notify.assert_not_awaited()
    dispatch.assert_not_called()
    db.finish_project_loop_handoff.assert_not_awaited()
    assert asyncio.current_task() is not None
    assert asyncio.current_task().cancelling() == 0

    # A later reconciliation item/tick can still execute in this same caller.
    settled = {"actions": ["next item settled"]}
    next_tail = AsyncMock(return_value=settled)
    db.renew_project_loop_handoff.side_effect = None
    db.renew_project_loop_handoff.return_value = True
    db.finish_project_loop_handoff.return_value = settled
    monkeypatch.setattr(
        orchestrator.main, "_handoff_atomic_project_loop_advance", next_tail
    )
    assert (
        await orchestrator.main._execute_persisted_project_loop_handoff(origin, output)
        == settled
    )
    next_tail.assert_awaited_once_with(origin, output, authority_check=ANY)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["resume_finalizer", "park_alert", "alert_only"])
async def test_expired_or_parked_route_nudges_then_sweeper_synthesizes(
    monkeypatch,
    route,
):
    """The actual sweeper→main path defers only a live stand-down route."""

    member_id, loop_id = str(uuid4()), str(uuid4())
    job = {
        "id": member_id,
        "status": "completed",
        "context": {"loop_id": loop_id},
    }
    loop = {
        "id": loop_id,
        "status": "running",
        "current_job_id": member_id,
        "current_stage_jobs": [member_id],
    }
    db = AsyncMock()
    db.list_running_project_loops.return_value = [loop]
    db.project_loop_members_have_live_completion_command.return_value = False
    db.get_loop_stage_member_statuses.return_value = {member_id: "completed"}
    db.get_job.return_value = job
    router = SimpleNamespace(
        enqueue_job=AsyncMock(return_value=SimpleNamespace(route=route, legacy=False))
    )
    prepared = {"kind": "mutation"}
    output = {
        "applicable": True,
        "won": True,
        "loop_id": loop_id,
        "completed_member_id": member_id,
    }
    prepare = AsyncMock(return_value=prepared)
    materialize = AsyncMock(return_value=output)
    handoff = AsyncMock(return_value={"actions": ["next stage"]})
    monkeypatch.setattr(orchestrator.main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    monkeypatch.setattr(
        orchestrator.main, "_get_completion_sweep_router", lambda: router
    )
    monkeypatch.setattr(
        orchestrator.main, "_prepare_atomic_project_loop_advance", prepare
    )
    monkeypatch.setattr(
        orchestrator.main, "_materialize_prepared_project_loop_advance", materialize
    )
    monkeypatch.setattr(
        orchestrator.main, "_execute_persisted_project_loop_handoff", handoff
    )

    assert (
        await _sweep_tick(
            db,
            orchestrator.main._advance_project_loop,
            completion_commands_enabled=True,
        )
        == 1
    )
    router.enqueue_job.assert_awaited_once_with(
        member_id, source="project_loop_advance"
    )
    prepare.assert_awaited_once_with(job, {})
    materialize.assert_awaited_once_with(prepared, job)
    handoff.assert_awaited_once_with(job, output)


@pytest.mark.asyncio
async def test_main_loop_synthesizer_stands_down_on_live_route(monkeypatch):
    member_id, loop_id = str(uuid4()), str(uuid4())
    job = {"id": member_id, "context": {"loop_id": loop_id}}
    router = SimpleNamespace(
        enqueue_job=AsyncMock(
            return_value=SimpleNamespace(route="stand_down", legacy=False)
        )
    )
    prepare = AsyncMock()
    monkeypatch.setattr(orchestrator.main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(
        orchestrator.main, "_get_completion_sweep_router", lambda: router
    )
    monkeypatch.setattr(
        orchestrator.main, "_prepare_atomic_project_loop_advance", prepare
    )

    await orchestrator.main._advance_project_loop(job, {}, [])

    router.enqueue_job.assert_awaited_once_with(
        member_id, source="project_loop_advance"
    )
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_commandless_reconciler_uses_full_persisted_output(monkeypatch):
    output = {
        "applicable": True,
        "won": True,
        "loop_id": str(uuid4()),
        "completed_member_id": str(uuid4()),
        "spawned_job_ids": [str(uuid4())],
        "total_jobs_run": 2,
        "replay": {"kb_ttl_decrement": True, "notifications": []},
    }
    origin = _marker_job(output)
    db = AsyncMock()
    db.list_pending_project_loop_handoffs.return_value = [origin]
    execute = AsyncMock(return_value={"actions": []})
    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    monkeypatch.setattr(
        orchestrator.main, "_execute_persisted_project_loop_handoff", execute
    )

    assert await orchestrator.main._reconcile_atomic_project_loop_handoff() == 1
    execute.assert_awaited_once_with(origin, output)


@pytest.mark.asyncio
async def test_command_owned_reconciler_routes_finalizer_never_parallel_tail(
    monkeypatch,
):
    output = {"applicable": True, "won": True, "loop_id": str(uuid4())}
    origin = _marker_job(output, command_id=str(uuid4()))
    db = AsyncMock()
    db.list_pending_project_loop_handoffs.return_value = [origin]
    router = SimpleNamespace(
        route_job=AsyncMock(return_value=SimpleNamespace(legacy=False))
    )
    execute = AsyncMock()
    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    monkeypatch.setattr(
        orchestrator.main, "_get_completion_sweep_router", lambda: router
    )
    monkeypatch.setattr(
        orchestrator.main, "_execute_persisted_project_loop_handoff", execute
    )

    assert await orchestrator.main._reconcile_atomic_project_loop_handoff() == 0
    router.route_job.assert_awaited_once_with(
        origin["id"], source="project_loop_handoff"
    )
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_lost_provisioning_replays_exact_persisted_ids(monkeypatch):
    loop_id, member_id, successor_id = (str(uuid4()) for _ in range(3))
    loop = {"id": loop_id, "project_id": None, "owner_id": None}
    successor = {
        "id": successor_id,
        "context": {"cloud_baseline": {"state": "seeding"}},
    }
    output = {
        "applicable": True,
        "won": True,
        "loop_id": loop_id,
        "completed_member_id": member_id,
        "spawned_job_ids": [successor_id],
        "total_jobs_run": 2,
        "replay": {
            "record_member": {"failed": False, "last_error": None},
            "notify_user_questions": False,
            "notifications": [],
            "close_ticket": None,
            "kb_ttl_decrement": False,
            "officer": None,
            "pre_actions": [],
            "action": {"kind": "rotation", "stage": "critic"},
        },
    }
    db = AsyncMock()
    db.get_project_loop.return_value = loop
    db.get_job.return_value = successor
    provisioned: set[str] = set()

    async def provision_once(*, job_row, **_kwargs):
        provisioned.add(str(job_row["id"]))
        return job_row

    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    monkeypatch.setattr(orchestrator.main, "vector_db", None)
    monkeypatch.setattr(orchestrator.main, "_record_loop_job_outcome", AsyncMock())
    monkeypatch.setattr(orchestrator.main, "_trigger_dispatch", MagicMock())
    monkeypatch.setattr(
        "orchestrator.services.job_provisioning.provision_job_repo", provision_once
    )

    # Models crash/response loss after provisioning but before the predecessor
    # marker/effect acknowledgment. The retry targets the same committed ID.
    await orchestrator.main._handoff_atomic_project_loop_advance(
        {"id": member_id, "context": {}}, output
    )
    await orchestrator.main._handoff_atomic_project_loop_advance(
        {"id": member_id, "context": {}}, output
    )
    assert provisioned == {successor_id}
    assert [str(call.args[0]) for call in db.get_job.await_args_list] == [
        successor_id,
        successor_id,
    ]


@pytest.mark.asyncio
async def test_multibyte_campaign_replay_and_final_action_are_bounded(monkeypatch):
    member_id = str(uuid4())
    huge = "界" * 20_000
    loop = {
        "id": str(uuid4()),
        "status": "running",
        "scheduling": "campaign",
        "role_sequence": ["critic", "developer"],
        "seq_index": 1,
        "total_jobs_run": 1,
        "remaining_iterations": 5,
        "max_iterations": 6,
        "run_until": None,
        "max_consecutive_failures": 3,
        "consecutive_failures": 0,
        "current_job_id": member_id,
        "current_stage_jobs": [member_id],
        "campaign": {
            "id": "campaign-a",
            "title": huge,
            "status": "active",
            "cursor": 1,
            "stages_done": 0,
            "member_failures": 0,
            "stages": [{"role": "developer"}, {"role": "critic"}],
        },
        "campaign_history": [],
    }
    mutation = plan_loop_advance(
        loop,
        completed_job={"id": member_id},
        completed_context={
            "loop_id": loop["id"],
            "loop_campaign_id": "campaign-a",
            "loop_campaign_index": 0,
        },
        member_states={member_id: "completed"},
        failed=False,
        member_error=None,
        deadline_passed=False,
    )
    output = {
        "applicable": True,
        "won": True,
        "loop_id": loop["id"],
        "completed_member_id": member_id,
        "spawned_job_ids": [str(uuid4())],
        "spawned_roles": ["critic"],
        "loop_status": "running",
        "seq_index": 1,
        "remaining_iterations": 4,
        "total_jobs_run": 2,
        "replay": dict(mutation.replay),
    }
    detail = _bounded_effect_detail("project_loop_advance", output)
    assert len(detail.encode("utf-8")) <= EFFECT_DETAIL_LIMIT_BYTES
    assert len(output["replay"]["action"]["label"].encode("utf-8")) <= 256
    assert huge not in detail

    # Exercise the real handoff projection: it uses the bounded label and a
    # final per-action byte ceiling before the command outcome sees the text.
    successor = {"id": output["spawned_job_ids"][0], "context": {}}
    db = AsyncMock()
    db.get_project_loop.return_value = {**loop, "project_id": None, "owner_id": None}
    db.get_job.return_value = successor
    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    monkeypatch.setattr(orchestrator.main, "vector_db", None)
    monkeypatch.setattr(orchestrator.main, "_record_loop_job_outcome", AsyncMock())
    monkeypatch.setattr(orchestrator.main, "_trigger_dispatch", MagicMock())
    monkeypatch.setattr(
        "orchestrator.services.job_provisioning.provision_job_repo",
        AsyncMock(return_value=successor),
    )
    projected = await orchestrator.main._handoff_atomic_project_loop_advance(
        {"id": member_id, "context": {}}, output
    )
    assert projected["actions"]
    assert all(len(action.encode("utf-8")) <= 768 for action in projected["actions"])
    assert len(json.dumps(output, ensure_ascii=False).encode("utf-8")) < 8192


def test_multibyte_plan_title_and_notes_are_bounded_at_replay_sources():
    member_id = str(uuid4())
    huge = "界" * 20_000
    loop = {
        "id": str(uuid4()),
        "status": "running",
        "scheduling": "campaign",
        "role_sequence": ["critic", "developer"],
        "seq_index": 0,
        "total_jobs_run": 3,
        "remaining_iterations": 6,
        "max_iterations": 9,
        "run_until": None,
        "max_consecutive_failures": 3,
        "consecutive_failures": 0,
        "current_job_id": member_id,
        "current_stage_jobs": [member_id],
        "campaign": {
            "id": "old-campaign",
            "title": huge,
            "status": "review",
            "cursor": 1,
            "stages_done": 1,
            "stages": [{"role": "developer"}],
            "initiative_note_id": "old-note",
        },
        "campaign_history": [],
    }
    mutation = plan_loop_advance(
        loop,
        completed_job={"id": member_id},
        completed_context={
            "loop_id": loop["id"],
            "loop_role": "critic",
            "loop_seq_index": 0,
            "loop_plan": {
                "disposition": {"outcome": "ship", "notes": huge},
                "initiative": {"kb_note_id": "next-note", "title": huge},
                "stages": [{"role": "developer"}],
                "acceptance": [huge],
            },
        },
        member_states={member_id: "completed"},
        failed=False,
        member_error=None,
        deadline_passed=False,
    )
    replay = json.dumps(dict(mutation.replay), ensure_ascii=False)
    assert len(replay.encode("utf-8")) < 8 * 1024
    assert huge not in replay
    assert all(
        len(notification["subject"].encode("utf-8")) <= 256
        and len(notification["message"].encode("utf-8")) <= 1024
        for notification in mutation.replay["notifications"]
    )
    assert len(mutation.replay["action"]["label"].encode("utf-8")) <= 256
