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


def _advance_db(loop: dict, jobs: list[dict], *, barrier: bool = True) -> AsyncMock:
    db = AsyncMock()
    db.get_project_loop.return_value = loop
    db.claim_project_loop_stage_barrier.return_value = barrier
    db.get_loop_stage_member_statuses.return_value = {
        str(j["id"]): j["status"] for j in jobs
    }
    return db


def _advance_patches(stack: ExitStack, db: AsyncMock, *, rotate: AsyncMock | None):
    stack.enter_context(patch("main.postgres_db", db))
    stack.enter_context(
        patch("main._merge_and_retro_loop_job", AsyncMock(return_value=("skipped", None)))
    )
    stack.enter_context(patch("main._notify_loop_user_questions", AsyncMock()))
    if rotate is not None:
        stack.enter_context(patch("main._rotate_loop_to_next_stage", rotate))


class TestUnifiedAdvance:
    @pytest.mark.asyncio
    async def test_width1_member_advances_through_barrier(self):
        job = _job()
        loop = _loop(current_job_id=job["id"], current_stage_jobs=[job["id"]])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        db.claim_project_loop_stage_barrier.assert_awaited_once_with(
            LOOP_ID, job["id"]
        )
        kw = rotate.await_args.kwargs
        assert kw["completed_job"] is job
        assert kw["completed_ctx"]["loop_id"] == LOOP_ID
        assert kw["completed_failed"] is False
        assert kw["next_remaining"] == 5  # 6 - 1, charged at the barrier
        assert kw["consecutive"] == 0

    @pytest.mark.asyncio
    async def test_nonmember_hook_is_a_noop(self):
        member, stray = _job(), _job()
        loop = _loop(current_stage_jobs=[member["id"]], current_job_id=member["id"])
        db = _advance_db(loop, [member, stray])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(stray, {}, [])
        db.claim_project_loop_stage_barrier.assert_not_awaited()
        rotate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_pointer_only_row_is_a_noop(self):
        # Pre-0063 shape (pointer set, empty membership) is NOT advanced by
        # the engine — the migration backfill / sweeper adopt branch owns it.
        job = _job()
        loop = _loop(current_job_id=job["id"], current_stage_jobs=[])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        db.claim_project_loop_stage_barrier.assert_not_awaited()
        rotate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lost_barrier_race_backs_off_after_merge(self):
        job = _job()
        loop = _loop(current_stage_jobs=[job["id"]], current_job_id=job["id"])
        db = _advance_db(loop, [job], barrier=False)
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        rotate.assert_not_awaited()
        db.update_project_loop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_budget_stop_clears_both_pointer_columns(self):
        job = _job()
        loop = _loop(
            remaining_iterations=1,
            current_stage_jobs=[job["id"]],
            current_job_id=job["id"],
        )
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        rotate.assert_not_awaited()
        kw = db.update_project_loop.call_args.kwargs
        assert kw["status"] == "completed" and kw["stop_reason"] == "budget"
        assert kw["current_job_id"] is None
        assert kw["current_stage_jobs"] == []

    @pytest.mark.asyncio
    async def test_width1_failure_keeps_specific_error_and_increments(self):
        job = _job(status="failed")
        loop = _loop(current_stage_jobs=[job["id"]], current_job_id=job["id"])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {"error": "kaboom"}, [])
        kw = rotate.await_args.kwargs
        assert kw["consecutive"] == 1
        assert kw["last_error"] == "kaboom"  # not the fan-out aggregate string
        assert kw["completed_failed"] is True

    @pytest.mark.asyncio
    async def test_fanout_partial_failure_resets_consecutive(self):
        ok = _job(role="scholar")
        bad = _job(role="product-qa", status="failed")
        loop = _loop(
            consecutive_failures=2,
            current_stage_jobs=[ok["id"], bad["id"]],
            current_job_id=None,
        )
        db = _advance_db(loop, [ok, bad])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(ok, {}, [])
        kw = rotate.await_args.kwargs
        assert kw["consecutive"] == 0 and kw["last_error"] is None

    @pytest.mark.asyncio
    async def test_campaign_step_fires_from_the_barrier_path(self):
        # The [A1] regression this whole task exists for: a campaign job
        # completing through the (now only) barrier path must reach
        # _advance_planner_campaign with its own job + context.
        job = _job(role="critic", loop_seq_index=1)
        loop = _loop(
            scheduling="campaign",
            role_sequence=[["scholar", "product-qa"], "critic", "developer"],
            seq_index=1,
            current_stage_jobs=[job["id"]],
            current_job_id=job["id"],
        )
        db = _advance_db(loop, [job])
        planner = AsyncMock(return_value=(True, None))  # handled: member spawned
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=None)
            stack.enter_context(patch("main._advance_planner_campaign", planner))
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        planner.assert_awaited_once()
        kw = planner.await_args.kwargs
        assert kw["completed_job"] is job
        assert kw["completed_ctx"]["loop_role"] == "critic"
        assert kw["completed_failed"] is False


class TestResume:
    @pytest.mark.asyncio
    async def test_resume_readvances_only_terminal_members(self):
        done = _job(status="completed")
        running = _job(status="processing")
        loop = _loop(current_stage_jobs=[done["id"], running["id"]])
        db = AsyncMock()
        db.update_project_loop.return_value = loop
        db.get_project_loop.return_value = loop
        by_id = {done["id"]: done, running["id"]: running}
        db.get_job.side_effect = lambda jid: by_id.get(str(jid))
        adv = AsyncMock()
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", db))
            stack.enter_context(patch("main._advance_project_loop", adv))
            from main import _resume_project_loop

            await _resume_project_loop(LOOP_ID)
        adv.assert_awaited_once_with(done, {}, [])
