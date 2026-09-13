"""Focused contracts for M3 critic synthesizer handoff deference."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services import verification_workflow


def _dependencies(
    database,
    *,
    wake=None,
    dispatch=None,
    curation=None,
    notifier=None,
    wake_drain=None,
) -> verification_workflow.VerificationDependencies:
    return verification_workflow.VerificationDependencies(
        store=database,
        transaction=verification_workflow.VerificationTransactionPorts(
            revalidate_datasource_selection=AsyncMock(return_value=([], {})),
            datasource_selection_provenance=AsyncMock(return_value={}),
            resolve_workspace_contract=MagicMock(
                return_value=SimpleNamespace(assigned_backend="sandbox")
            ),
            deep_merge_dicts=MagicMock(side_effect=lambda left, right: left | right),
            is_lite_config_override=MagicMock(return_value=False),
        ),
        effects=verification_workflow.VerificationEffectPorts(
            forge=SimpleNamespace(is_initialized=False),
            notifier=notifier or MagicMock(),
            prepare_job_repository_authority=AsyncMock(),
            trigger_dispatch=dispatch or MagicMock(),
            maybe_wake_session=wake or AsyncMock(),
            kick_session_wake_drain=wake_drain or MagicMock(),
            trigger_curation_final_pass=curation or AsyncMock(),
            set_target_to_autonomy_status=AsyncMock(return_value="completed"),
            escalate_target=AsyncMock(return_value="pending_review"),
            internal_resume_job=AsyncMock(return_value=True),
        ),
    )


@pytest.mark.asyncio
async def test_lost_s27_world_cas_has_no_external_followups():
    database = MagicMock()
    database.get_job = AsyncMock()
    wake = AsyncMock()
    dispatch = MagicMock()
    curation = AsyncMock()
    dependencies = _dependencies(
        database, wake=wake, dispatch=dispatch, curation=curation
    )

    result = await verification_workflow.run_critic_verdict_followups(
        {
            "applicable": True,
            "world_cas_won": False,
            "target_job_id": "target",
            "critic_job_id": "critic",
        },
        completion_command_id="command",
        dependencies=dependencies,
    )

    assert result == {"actions": []}
    database.get_job.assert_not_awaited()
    wake.assert_not_awaited()
    dispatch.assert_not_called()
    curation.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_s30_world_cas_has_no_external_handoff(monkeypatch):
    database = MagicMock()
    database.get_job = AsyncMock()
    workspace_handoff = AsyncMock()
    dispatch = MagicMock()
    wake = AsyncMock()
    monkeypatch.setattr(
        verification_workflow,
        "setup_verification_critic_workspace",
        workspace_handoff,
    )
    dependencies = _dependencies(database, wake=wake, dispatch=dispatch)

    result = await verification_workflow.run_verification_critic_handoff(
        {
            "applicable": True,
            "world_cas_won": False,
            "action": "handoff",
            "target_job_id": "target",
            "critic_job_id": "critic",
        },
        dependencies=dependencies,
    )

    assert result == {"actions": []}
    database.get_job.assert_not_awaited()
    workspace_handoff.assert_not_awaited()
    dispatch.assert_not_called()
    wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_winning_s27_return_only_kicks_dispatch():
    database = MagicMock()
    database.get_job = AsyncMock(return_value={"id": "target"})
    dispatch = MagicMock()
    wake = AsyncMock()
    curation = AsyncMock()
    dependencies = _dependencies(
        database, wake=wake, dispatch=dispatch, curation=curation
    )

    result = await verification_workflow.run_critic_verdict_followups(
        {
            "applicable": True,
            "world_cas_won": True,
            "outcome": "returned",
            "new_status": "paused",
            "target_job_id": "target",
            "critic_job_id": "critic",
            "open_finding_count": 2,
        },
        completion_command_id="command",
        dependencies=dependencies,
    )

    assert result == {
        "actions": ["target target resumed with feedback from critic critic"]
    }
    dispatch.assert_called_once_with()
    wake.assert_not_awaited()
    curation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("followup", "plan"),
    [
        (
            "s27",
            {
                "applicable": True,
                "world_cas_won": True,
                "outcome": "escalate",
                "new_status": "pending_review",
                "target_job_id": "target",
                "critic_job_id": "critic",
            },
        ),
        (
            "s30",
            {
                "applicable": True,
                "world_cas_won": True,
                "action": "escalate",
                "action_code": "verification_gate",
                "new_status": "pending_review",
                "target_job_id": "target",
            },
        ),
    ],
)
async def test_critic_escalation_followups_bound_multibyte_reason_and_action(
    followup, plan
):
    huge_reason = "検" * 20_000
    database = MagicMock()
    database.get_job = AsyncMock(
        return_value={
            "id": "target",
            "status": "pending_review",
            "error_message": huge_reason,
            "user_id": "user",
            "config_name": "defaults",
            "context": {},
        }
    )
    notifier = MagicMock()
    notifier.record_review_returned = AsyncMock()
    wake = AsyncMock()
    wake_drain = MagicMock()
    dependencies = _dependencies(
        database, wake=wake, notifier=notifier, wake_drain=wake_drain
    )

    if followup == "s27":
        result = await verification_workflow.run_critic_verdict_followups(
            plan,
            completion_command_id="command",
            dependencies=dependencies,
        )
    else:
        result = await verification_workflow.run_verification_critic_handoff(
            plan, dependencies=dependencies
        )

    notified_reason = notifier.record_review_returned.await_args.kwargs["reason"]
    assert len(notified_reason.encode("utf-8")) <= 1024
    assert notified_reason.endswith("…")
    assert len(result["actions"][0].encode("utf-8")) <= 1024
