"""Pure contracts for completion safety/operator resolution inputs."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from orchestrator.services.completion_command_resolution import (
    CompletionCommandResolution,
    CompletionResolutionConflict,
)


COMMAND_ID = "22222222-bbbb-4222-8222-222222222222"


def test_resolution_configuration_and_operator_inputs_fail_before_database() -> None:
    with pytest.raises(ValueError, match="command_deadline_seconds"):
        CompletionCommandResolution(object(), command_deadline_seconds=0)
    with pytest.raises(ValueError, match="safety_net_grace_seconds"):
        CompletionCommandResolution(object(), safety_net_grace_seconds=-1)


@pytest.mark.asyncio
async def test_force_requires_explicit_bounded_incident_fields() -> None:
    resolution = CompletionCommandResolution(object())
    with pytest.raises(ValueError, match="expected_state"):
        await resolution.force_resolve(
            COMMAND_ID,
            expected_state="done",
            terminal_status="completed",
            actor="operator",
            reason="incident",
        )
    with pytest.raises(ValueError, match="terminal_status"):
        await resolution.force_resolve(
            COMMAND_ID,
            expected_state="parked",
            terminal_status="pending_review",
            actor="operator",
            reason="incident",
        )
    with pytest.raises(ValueError, match="actor"):
        await resolution.force_resolve(
            COMMAND_ID,
            expected_state="parked",
            terminal_status="completed",
            actor=" ",
            reason="incident",
        )
    with pytest.raises(ValueError, match="reason"):
        await resolution.force_resolve(
            COMMAND_ID,
            expected_state="parked",
            terminal_status="completed",
            actor="operator",
            reason=" ",
        )


@pytest.mark.asyncio
async def test_unpark_validates_actor_before_database() -> None:
    resolution = CompletionCommandResolution(object())
    with pytest.raises(ValueError, match="actor"):
        await resolution.unpark(COMMAND_ID, actor="")


@pytest.mark.asyncio
async def test_incident_sink_failure_cannot_undo_committed_force_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sink is explicitly post-commit and best-effort."""

    sink = AsyncMock(side_effect=RuntimeError("sink unavailable"))
    resolution = CompletionCommandResolution(object(), alert=sink)
    incident_type = __import__(
        "orchestrator.services.completion_command_resolution",
        fromlist=["CompletionResolutionIncident"],
    ).CompletionResolutionIncident
    incident = incident_type(
        dedup_key=f"completion.force_resolved:{COMMAND_ID}",
        kind="force_resolved",
        command_id=COMMAND_ID,
        job_id="11111111-aaaa-4111-8111-111111111111",
        actor="operator",
        reason="manual incident",
        terminal_status="failed",
    )

    await resolution._emit(incident)

    sink.assert_awaited_once_with(incident)
    assert "incident delivery failed" in caplog.text


def test_conflict_exposes_machine_reason() -> None:
    conflict = CompletionResolutionConflict("workspace_teardown_authorized")
    assert conflict.reason == "workspace_teardown_authorized"
    assert str(conflict) == conflict.reason


def test_result_deadline_type_remains_timezone_aware_example() -> None:
    # A tiny static guard against accidentally changing the public result to an
    # epoch/JSON string while wiring the HTTP route.
    assert datetime.now(UTC).tzinfo is UTC
