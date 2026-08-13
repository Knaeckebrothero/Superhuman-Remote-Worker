"""Focused contracts for M3 critic synthesizer handoff deference."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import main


@pytest.mark.asyncio
async def test_lost_s27_world_cas_has_no_external_followups(monkeypatch):
    database = MagicMock()
    database.get_job = AsyncMock()
    wake = AsyncMock()
    dispatch = MagicMock()
    curation = AsyncMock()
    monkeypatch.setattr(main, "postgres_db", database)
    monkeypatch.setattr(main, "maybe_wake_session", wake)
    monkeypatch.setattr(main, "_trigger_dispatch", dispatch)
    monkeypatch.setattr(main, "_trigger_curation_final_pass", curation)

    result = await main._run_critic_verdict_followups(
        {
            "applicable": True,
            "world_cas_won": False,
            "target_job_id": "target",
            "critic_job_id": "critic",
        },
        completion_command_id="command",
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
    monkeypatch.setattr(main, "postgres_db", database)
    monkeypatch.setattr(main, "_setup_verification_critic_workspace", workspace_handoff)
    monkeypatch.setattr(main, "_trigger_dispatch", dispatch)
    monkeypatch.setattr(main, "maybe_wake_session", wake)

    result = await main._run_verification_critic_handoff(
        {
            "applicable": True,
            "world_cas_won": False,
            "action": "handoff",
            "target_job_id": "target",
            "critic_job_id": "critic",
        }
    )

    assert result == {"actions": []}
    database.get_job.assert_not_awaited()
    workspace_handoff.assert_not_awaited()
    dispatch.assert_not_called()
    wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_winning_s27_return_only_kicks_dispatch(monkeypatch):
    database = MagicMock()
    database.get_job = AsyncMock(return_value={"id": "target"})
    dispatch = MagicMock()
    wake = AsyncMock()
    curation = AsyncMock()
    monkeypatch.setattr(main, "postgres_db", database)
    monkeypatch.setattr(main, "_trigger_dispatch", dispatch)
    monkeypatch.setattr(main, "maybe_wake_session", wake)
    monkeypatch.setattr(main, "_trigger_curation_final_pass", curation)

    result = await main._run_critic_verdict_followups(
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
    monkeypatch, followup, plan
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
    notifier.notify_review_returned_to_manual = AsyncMock()
    wake = AsyncMock()
    wake_drain = MagicMock()
    monkeypatch.setattr(main, "postgres_db", database)
    monkeypatch.setattr(main, "notification_service", notifier)
    monkeypatch.setattr(main, "maybe_wake_session", wake)
    monkeypatch.setattr(main, "_kick_session_wake_drain", wake_drain)

    if followup == "s27":
        result = await main._run_critic_verdict_followups(
            plan,
            completion_command_id="command",
        )
    else:
        result = await main._run_verification_critic_handoff(plan)

    notified_reason = notifier.notify_review_returned_to_manual.await_args.kwargs[
        "reason"
    ]
    assert len(notified_reason.encode("utf-8")) <= 1024
    assert notified_reason.endswith("…")
    assert len(result["actions"][0].encode("utf-8")) <= 1024
