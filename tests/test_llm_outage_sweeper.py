"""Tests for the LLM-outage re-dispatch sweeper tick.

Exercises the control flow of ``_llm_outage_sweep_once`` — ceiling backstop and
CAS-guarded re-dispatch — against a mocked ``postgres_db``.
Live DB round-trips are covered by the k3d E2E in
docs/features/llm_outage_pause_and_backoff_redispatch.md.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import orchestrator.main as main  # noqa: E402
from orchestrator.services.completion import (  # noqa: E402
    LLM_OUTAGE_CEILING_SECONDS,
)


def _due_job(job_id, *, first_ago, last_ago=60, attempt=3):
    """A paused llm_unavailable job whose backoff is due (real-now relative)."""
    real_now = datetime.now(timezone.utc)
    return {
        "id": job_id,
        "config_name": "loop",
        "user_id": None,
        "project_id": None,
        "freeze_data": {
            "freeze_type": "llm_unavailable",
            "classification": "rate_limit",
            "next_retry_at": real_now.isoformat(),
            "attempt": attempt,
        },
        "context": {
            "llm_outage": {
                "attempt": attempt,
                "first_failed_at": (
                    real_now - timedelta(seconds=first_ago)
                ).isoformat(),
                "last_failed_at": (real_now - timedelta(seconds=last_ago)).isoformat(),
            }
        },
    }


@pytest.fixture
def wired(monkeypatch):
    """Patch the module globals the sweeper reaches; return (db, trigger)."""
    db = MagicMock()
    db.list_due_llm_outage_jobs = AsyncMock(return_value=[])
    db.claim_llm_outage_redispatch = AsyncMock(return_value=True)
    db.fail_llm_outage_job = AsyncMock(return_value=True)
    trigger = MagicMock()
    monkeypatch.setattr(main, "postgres_db", db)
    monkeypatch.setattr(main, "_trigger_dispatch", trigger)
    monkeypatch.setattr(main, "_notify_operator_freeze", AsyncMock())
    return db, trigger


@pytest.mark.asyncio
async def test_no_due_jobs_no_dispatch(wired):
    db, trigger = wired
    assert await main._llm_outage_sweep_once() == (0, 0)
    trigger.assert_not_called()


@pytest.mark.asyncio
async def test_due_under_ceiling_redispatches(wired):
    db, trigger = wired
    db.list_due_llm_outage_jobs = AsyncMock(
        return_value=[_due_job("j1", first_ago=3600)]
    )
    assert await main._llm_outage_sweep_once() == (1, 0)
    db.claim_llm_outage_redispatch.assert_awaited_once_with("j1")
    db.fail_llm_outage_job.assert_not_awaited()
    trigger.assert_called_once()


@pytest.mark.asyncio
async def test_cas_lost_not_counted(wired):
    # Another sweeper (transient dual-leader) already claimed it → CAS returns False.
    db, trigger = wired
    db.list_due_llm_outage_jobs = AsyncMock(
        return_value=[_due_job("j1", first_ago=3600)]
    )
    db.claim_llm_outage_redispatch = AsyncMock(return_value=False)
    assert await main._llm_outage_sweep_once() == (0, 0)
    trigger.assert_not_called()


@pytest.mark.asyncio
async def test_over_ceiling_fails_not_redispatched(wired):
    db, trigger = wired
    db.list_due_llm_outage_jobs = AsyncMock(
        return_value=[_due_job("j1", first_ago=LLM_OUTAGE_CEILING_SECONDS + 3600)]
    )
    assert await main._llm_outage_sweep_once() == (0, 1)
    db.fail_llm_outage_job.assert_awaited_once()
    db.claim_llm_outage_redispatch.assert_not_awaited()
    trigger.assert_not_called()

