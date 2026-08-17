"""Tests for the LLM-outage re-dispatch sweeper tick.

Exercises the control flow of ``_llm_outage_sweep_once`` — ceiling backstop and
CAS-guarded re-dispatch — against a mocked ``postgres_db``.
Live DB round-trips are covered by the k3d E2E in
knowledge-base/knowledge/features/llm_outage_pause_and_backoff_redispatch.md.
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
    db.list_due_llm_outage_jobs.assert_awaited_once_with(
        limit=50,
        completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED,
    )
    trigger.assert_not_called()


@pytest.mark.asyncio
async def test_due_under_ceiling_redispatches(wired):
    db, trigger = wired
    db.list_due_llm_outage_jobs = AsyncMock(
        return_value=[_due_job("j1", first_ago=3600)]
    )
    assert await main._llm_outage_sweep_once() == (1, 0)
    db.claim_llm_outage_redispatch.assert_awaited_once_with(
        "j1", completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED
    )
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
    assert db.fail_llm_outage_job.await_args.args[0] == "j1"
    assert "past the give-up ceiling" in db.fail_llm_outage_job.await_args.args[1]
    assert db.fail_llm_outage_job.await_args.kwargs == {
        "completion_commands_enabled": main.COMPLETION_COMMANDS_ENABLED
    }
    db.claim_llm_outage_redispatch.assert_not_awaited()
    trigger.assert_not_called()


# ---------------------------------------------------------------------------
# Sweep-fail parent unblock — a fail_llm_outage_job write bypasses /complete,
# so the subjob unblock handlers must run here or a ceiling-failed scholar
# strands its 'waiting' parent forever
# (knowledge-base/knowledge/features/llm_outage_subjob_resilience.md, design #4).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ceiling_failed_subjob_runs_parent_unblock_handlers(wired, monkeypatch):
    db, trigger = wired
    job = _due_job("sub-1", first_ago=LLM_OUTAGE_CEILING_SECONDS + 3600)
    job["parent_job_id"] = "par-1"
    job["creation_order"] = 0
    db.list_due_llm_outage_jobs = AsyncMock(return_value=[job])
    scholar = AsyncMock()
    delegation = AsyncMock()
    monkeypatch.setattr(main, "_handle_scholar_completion", scholar)
    monkeypatch.setattr(main, "_handle_delegation_child_completion", delegation)

    assert await main._llm_outage_sweep_once() == (0, 1)
    scholar.assert_awaited_once()
    delegation.assert_awaited_once()
    # Handlers must see the post-fail status, not the paused sweep row —
    # _handle_scholar_completion keys is_failure on it.
    assert scholar.await_args.args[0]["status"] == "failed"
    assert delegation.await_args.args[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_ceiling_failed_toplevel_skips_subjob_handlers(wired, monkeypatch):
    db, trigger = wired
    db.list_due_llm_outage_jobs = AsyncMock(
        return_value=[_due_job("j1", first_ago=LLM_OUTAGE_CEILING_SECONDS + 3600)]
    )
    scholar = AsyncMock()
    delegation = AsyncMock()
    monkeypatch.setattr(main, "_handle_scholar_completion", scholar)
    monkeypatch.setattr(main, "_handle_delegation_child_completion", delegation)

    assert await main._llm_outage_sweep_once() == (0, 1)
    scholar.assert_not_awaited()
    delegation.assert_not_awaited()


@pytest.mark.asyncio
async def test_unblock_handler_error_does_not_break_sweep(wired, monkeypatch):
    # A handler blow-up must not abort the tick — later due jobs still process.
    db, trigger = wired
    sub = _due_job("sub-1", first_ago=LLM_OUTAGE_CEILING_SECONDS + 3600)
    sub["parent_job_id"] = "par-1"
    sub["creation_order"] = 0
    ok = _due_job("j2", first_ago=3600)
    db.list_due_llm_outage_jobs = AsyncMock(return_value=[sub, ok])
    monkeypatch.setattr(
        main,
        "_handle_scholar_completion",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    monkeypatch.setattr(main, "_handle_delegation_child_completion", AsyncMock())

    assert await main._llm_outage_sweep_once() == (1, 1)


# ---------------------------------------------------------------------------
# Born-parked loop members (knowledge-base/knowledge/issues/loop_advances_into_active_model_cooldown.md)
# — created paused+frozen with NO first_failed_at, so the ceiling clock starts
# at wake, not at creation. This test fails if anyone ever stamps
# first_failed_at at park time: the whole park duration would read as elapsed
# outage and the member would be ceiling-killed at its wake instant.
# ---------------------------------------------------------------------------


def _born_parked_job(job_id):
    """A loop member created parked: due now, attempt 0, no first/last_failed_at."""
    real_now = datetime.now(timezone.utc)
    return {
        "id": job_id,
        "config_name": "critic",
        "user_id": None,
        "project_id": None,
        "freeze_data": {
            "freeze_type": "llm_unavailable",
            "classification": "cooldown",
            "next_retry_at": real_now.isoformat(),
            "attempt": 0,
            "origin": "loop_cooldown_park",
        },
        "context": {
            "llm_outage": {
                "attempt": 0,
                "next_retry_at": real_now.isoformat(),
            }
        },
    }


@pytest.mark.asyncio
async def test_born_parked_job_wakes_and_claims(wired):
    db, trigger = wired
    db.list_due_llm_outage_jobs = AsyncMock(return_value=[_born_parked_job("j-park")])
    assert await main._llm_outage_sweep_once() == (1, 0)
    db.claim_llm_outage_redispatch.assert_awaited_once_with(
        "j-park", completion_commands_enabled=main.COMPLETION_COMMANDS_ENABLED
    )
    db.fail_llm_outage_job.assert_not_awaited()
    trigger.assert_called_once()
