"""Unified loop engine, Phase 1 (docs/features/loop_unified_engine.md).

Every turn — width 1 included — is barrier-tracked in ``current_stage_jobs``:
``_writeback_loop_stage`` writes the membership plus the width-1 display
mirror, ``_advance_project_loop`` routes every member through the atomic
barrier, the winner threads its own job + context into the rotate (so the
campaign step fires from the barrier path), and stop-writes clear BOTH
pointer columns. The legacy single-job rotate path is gone.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

LOOP_ID = "105a6f98-134c-4077-b7e1-6d08916650d7"


def _job(*, role: str = "scholar", status: str = "completed", **ctx_over) -> dict:
    ctx = {
        "loop_id": LOOP_ID,
        "loop_role": role,
        "loop_iteration": 1,
        "loop_seq_index": 0,
        "loop_remaining": 5,
    }
    ctx.update(ctx_over)
    return {"id": str(uuid.uuid4()), "status": status, "context": ctx}


def _loop(**over) -> dict:
    base = {
        "id": LOOP_ID,
        "status": "running",
        "scheduling": "standard",
        "role_sequence": ["scholar", "critic", "developer"],
        "seq_index": 0,
        "total_jobs_run": 1,
        "max_iterations": 6,
        "remaining_iterations": 6,
        "max_consecutive_failures": 3,
        "consecutive_failures": 0,
        "current_job_id": None,
        "current_stage_jobs": [],
        "campaign": None,
        "campaign_history": [],
        "run_until": None,
        "project_id": None,
    }
    base.update(over)
    return base


class TestWritebackLoopStage:
    @pytest.mark.asyncio
    async def test_width1_writes_membership_and_display_mirror(self):
        db = AsyncMock()
        with patch("main.postgres_db", db):
            from main import _writeback_loop_stage

            await _writeback_loop_stage(
                LOOP_ID,
                jobs=[{"id": "job-1"}],
                seq_index=1,
                remaining=4,
                total=2,
                consecutive=0,
                last_error=None,
            )
        kw = db.update_project_loop.call_args.kwargs
        assert kw["current_stage_jobs"] == ["job-1"]
        assert kw["current_job_id"] == "job-1"
        assert kw["seq_index"] == 1 and kw["total_jobs_run"] == 2

    @pytest.mark.asyncio
    async def test_fanout_writes_membership_with_null_mirror(self):
        db = AsyncMock()
        with patch("main.postgres_db", db):
            from main import _writeback_loop_stage

            await _writeback_loop_stage(
                LOOP_ID,
                jobs=[{"id": "a"}, {"id": "b"}],
                seq_index=0,
                remaining=4,
                total=3,
                consecutive=0,
                last_error=None,
            )
        kw = db.update_project_loop.call_args.kwargs
        assert kw["current_stage_jobs"] == ["a", "b"]
        assert kw["current_job_id"] is None
