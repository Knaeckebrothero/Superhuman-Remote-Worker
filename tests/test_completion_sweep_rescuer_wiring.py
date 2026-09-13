"""Caller wiring for the completion-aware re-dispatch rescuers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services import completion_recovery


@pytest.mark.asyncio
async def test_infra_sweeper_threads_completion_commands_flag():
    db = MagicMock()
    db.list_due_backoff_jobs = AsyncMock(return_value=[{"id": "job-1"}])
    db.claim_backoff_redispatch = AsyncMock(return_value=True)
    trigger = MagicMock()
    commands_enabled = True
    dependencies = SimpleNamespace(
        store=db,
        completion_commands_enabled=lambda: commands_enabled,
        trigger_dispatch=trigger,
    )

    assert await completion_recovery.infra_transient_sweep_once(
        dependencies=dependencies
    ) == (1, 1)

    db.list_due_backoff_jobs.assert_awaited_once_with(
        "infra_transient",
        limit=50,
        completion_commands_enabled=commands_enabled,
    )
    db.claim_backoff_redispatch.assert_awaited_once_with(
        "job-1",
        "infra_transient",
        completion_commands_enabled=commands_enabled,
    )
    trigger.assert_called_once_with()
