"""Caller wiring for the completion-aware re-dispatch rescuers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import orchestrator.main as main


@pytest.mark.asyncio
async def test_infra_sweeper_threads_completion_commands_flag(monkeypatch):
    db = MagicMock()
    db.list_due_backoff_jobs = AsyncMock(return_value=[{"id": "job-1"}])
    db.claim_backoff_redispatch = AsyncMock(return_value=True)
    trigger = MagicMock()
    monkeypatch.setattr(main, "postgres_db", db)
    monkeypatch.setattr(main, "_trigger_dispatch", trigger)

    assert await main._infra_transient_sweep_once() == (1, 1)

    db.list_due_backoff_jobs.assert_awaited_once_with(
        "infra_transient",
        limit=50,
        completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED,
    )
    db.claim_backoff_redispatch.assert_awaited_once_with(
        "job-1",
        "infra_transient",
        completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED,
    )
    trigger.assert_called_once_with()
