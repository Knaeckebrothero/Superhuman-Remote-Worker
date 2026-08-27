"""Unified loop engine, Phase 1 (knowledge-base/knowledge/features/loop_unified_engine.md).

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
    # The cooldown-park aggregator refetches failed sibling rows; default to
    # "row gone" so tests that don't care read as no-park instead of leaking
    # AsyncMock children into the extractor.
    db.get_job.return_value = None
    return db


def _advance_patches(
    stack: ExitStack,
    db: AsyncMock,
    *,
    rotate: AsyncMock | None,
    notify: AsyncMock | None = None,
):
    stack.enter_context(patch("main.postgres_db", db))
    stack.enter_context(
        patch(
            "main._record_loop_job_outcome",
            AsyncMock(return_value=("no-changes", None)),
        )
    )
    stack.enter_context(patch("main._notify_loop_user_questions", AsyncMock()))
    stack.enter_context(patch("main._notify_loop_event", notify or AsyncMock()))
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
        db.claim_project_loop_stage_barrier.assert_awaited_once_with(LOOP_ID, job["id"])
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


# =============================================================================
# Born-parked spawn on model-cooldown turn failure
# (knowledge-base/knowledge/issues/loop_advances_into_active_model_cooldown.md)
# =============================================================================


def _cooldown_error(reset_at: float, message: str = "cool") -> dict:
    return {
        "message": message,
        "type": "llm_error",
        "recoverable": False,
        "classification": "cooldown",
        "model": "gpt-5.3-codex-spark",
        "reset_at": reset_at,
    }


class TestCooldownPark:
    @pytest.mark.asyncio
    async def test_cooldown_failed_member_parks_next_spawn(self):
        import time as _time

        job = _job(status="failed")
        loop = _loop(current_stage_jobs=[job["id"]], current_job_id=job["id"])
        db = _advance_db(loop, [job])
        rotate, notify = AsyncMock(), AsyncMock()
        reset_at = _time.time() + 7200
        actions: list = []
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate, notify=notify)
            from main import _advance_project_loop

            await _advance_project_loop(
                job, {"error": _cooldown_error(reset_at)}, actions
            )
        kw = rotate.await_args.kwargs
        assert kw["park_until"] is not None
        assert abs(kw["park_until"].timestamp() - reset_at) < 2
        assert kw["consecutive"] == 1
        assert kw["last_error"] == "cool"  # human string, not the dict
        assert kw["completed_failed"] is True
        assert any("parked until" in a for a in actions)
        notify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cooldown_reset_in_past_spawns_normally(self):
        import time as _time

        job = _job(status="failed")
        loop = _loop(current_stage_jobs=[job["id"]], current_job_id=job["id"])
        db = _advance_db(loop, [job])
        rotate, notify = AsyncMock(), AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate, notify=notify)
            from main import _advance_project_loop

            await _advance_project_loop(
                job, {"error": _cooldown_error(_time.time() - 60)}, []
            )
        assert rotate.await_args.kwargs["park_until"] is None
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_noncooldown_dict_error_spawns_normally(self):
        job = _job(status="failed")
        loop = _loop(current_stage_jobs=[job["id"]], current_job_id=job["id"])
        db = _advance_db(loop, [job])
        rotate, notify = AsyncMock(), AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate, notify=notify)
            from main import _advance_project_loop

            await _advance_project_loop(
                job, {"error": {"message": "boom", "type": "llm_error"}}, []
            )
        kw = rotate.await_args.kwargs
        assert kw["park_until"] is None
        assert kw["last_error"] == "boom"
        notify.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heal_redrive_reads_persisted_error_details(self):
        import json as _json
        import time as _time

        reset_at = _time.time() + 3600
        job = _job(status="failed")
        # asyncpg hands JSONB back as a raw JSON string on the heal path.
        job["error_details"] = _json.dumps(
            {"classification": "cooldown", "reset_at": reset_at}
        )
        loop = _loop(current_stage_jobs=[job["id"]], current_job_id=job["id"])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        kw = rotate.await_args.kwargs
        assert kw["park_until"] is not None
        assert abs(kw["park_until"].timestamp() - reset_at) < 2

    @pytest.mark.asyncio
    async def test_fanout_park_uses_max_reset_among_cooldown_failed(self):
        import time as _time

        t1, t2 = _time.time() + 3600, _time.time() + 7200
        winner = _job(role="scholar", status="failed")
        sibling = _job(role="product-qa", status="failed")
        sibling_row = dict(
            sibling, error_details={"classification": "cooldown", "reset_at": t2}
        )
        loop = _loop(
            current_stage_jobs=[winner["id"], sibling["id"]], current_job_id=None
        )
        db = _advance_db(loop, [winner, sibling])
        db.get_job.side_effect = lambda jid: (
            sibling_row if str(jid) == sibling["id"] else None
        )
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(winner, {"error": _cooldown_error(t1)}, [])
        kw = rotate.await_args.kwargs
        assert abs(kw["park_until"].timestamp() - t2) < 2
        # Only the sibling needed a row refetch — the winner rode the payload.
        assert db.get_job.await_count == 1

    @pytest.mark.asyncio
    async def test_fanout_partial_failure_still_parks_but_resets_consecutive(self):
        import time as _time

        reset_at = _time.time() + 3600
        ok = _job(role="scholar")
        bad = _job(role="product-qa", status="failed")
        bad_row = dict(
            bad, error_details={"classification": "cooldown", "reset_at": reset_at}
        )
        loop = _loop(
            consecutive_failures=2,
            current_stage_jobs=[ok["id"], bad["id"]],
            current_job_id=None,
        )
        db = _advance_db(loop, [ok, bad])
        db.get_job.side_effect = lambda jid: (
            bad_row if str(jid) == bad["id"] else None
        )
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(ok, {}, [])
        kw = rotate.await_args.kwargs
        assert kw["consecutive"] == 0
        assert abs(kw["park_until"].timestamp() - reset_at) < 2

    @pytest.mark.asyncio
    async def test_park_clamped_to_cap(self):
        import time as _time

        from services.project_loops import LOOP_COOLDOWN_PARK_CAP_SECONDS

        job = _job(status="failed")
        loop = _loop(current_stage_jobs=[job["id"]], current_job_id=job["id"])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(
                job, {"error": _cooldown_error(_time.time() + 365 * 24 * 3600)}, []
            )
        park = rotate.await_args.kwargs["park_until"]
        assert (
            abs(park.timestamp() - (_time.time() + LOOP_COOLDOWN_PARK_CAP_SECONDS)) < 30
        )

    @pytest.mark.asyncio
    async def test_stop_axis_wins_over_park(self):
        import time as _time

        job = _job(status="failed")
        loop = _loop(
            consecutive_failures=2,
            max_consecutive_failures=3,
            current_stage_jobs=[job["id"]],
            current_job_id=job["id"],
        )
        db = _advance_db(loop, [job])
        rotate, notify = AsyncMock(), AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate, notify=notify)
            from main import _advance_project_loop

            await _advance_project_loop(
                job, {"error": _cooldown_error(_time.time() + 7200)}, []
            )
        rotate.assert_not_awaited()
        notify.assert_not_awaited()
        kw = db.update_project_loop.call_args.kwargs
        assert kw["status"] == "failed" and kw["stop_reason"] == "failures"


class TestParkThreading:
    @pytest.mark.asyncio
    async def test_rotate_threads_park_until_to_stage_spawn(self):
        from datetime import datetime, timezone

        park = datetime(2026, 7, 30, 11, 54, 4, tzinfo=timezone.utc)
        loop = _loop()
        db = AsyncMock()
        spawn = AsyncMock(return_value=([{"id": "j2"}], 2))
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", db))
            stack.enter_context(patch("main._spawn_loop_stage", spawn))
            stack.enter_context(patch("main._writeback_loop_stage", AsyncMock()))
            from main import _rotate_loop_to_next_stage

            await _rotate_loop_to_next_stage(
                loop,
                seq_index_completed=0,
                base_total=1,
                next_remaining=5,
                consecutive=0,
                last_error=None,
                actions=[],
                park_until=park,
            )
        assert spawn.await_args.kwargs["park_until"] == park

    @pytest.mark.asyncio
    async def test_rotate_threads_park_until_into_campaign_step(self):
        from datetime import datetime, timezone

        park = datetime(2026, 7, 30, 11, 54, 4, tzinfo=timezone.utc)
        job = _job(role="developer")
        loop = _loop(scheduling="campaign")
        planner = AsyncMock(return_value=(True, None))
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", AsyncMock()))
            stack.enter_context(patch("main._advance_planner_campaign", planner))
            from main import _rotate_loop_to_next_stage

            await _rotate_loop_to_next_stage(
                loop,
                seq_index_completed=0,
                base_total=1,
                next_remaining=5,
                consecutive=0,
                last_error=None,
                actions=[],
                completed_job=job,
                completed_ctx=job["context"],
                completed_failed=True,
                park_until=park,
            )
        assert planner.await_args.kwargs["park_until"] == park

    @pytest.mark.asyncio
    async def test_campaign_member_spawn_inherits_park(self):
        from datetime import datetime, timezone

        park = datetime(2026, 7, 30, 11, 54, 4, tzinfo=timezone.utc)
        loop = _loop(scheduling="campaign")
        campaign = {"id": "c1", "title": "t", "stages": ["developer"]}
        spawn = AsyncMock(return_value=([{"id": "j3"}], 2))
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", AsyncMock()))
            stack.enter_context(patch("main._spawn_loop_stage", spawn))
            stack.enter_context(patch("main._writeback_loop_stage", AsyncMock()))
            from main import _spawn_campaign_member

            await _spawn_campaign_member(
                loop,
                campaign=campaign,
                stage_index=0,
                execution_slot=2,
                base_total=1,
                next_remaining=5,
                consecutive=0,
                last_error=None,
                actions=[],
                park_until=park,
            )
        assert spawn.await_args.kwargs["park_until"] == park

    @pytest.mark.asyncio
    async def test_spawn_loop_job_forwards_park_to_create(self):
        from datetime import datetime, timezone

        park = datetime(2026, 7, 30, 11, 54, 4, tzinfo=timezone.utc)
        loop = _loop()
        create = AsyncMock(return_value={"id": "j4"})
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", AsyncMock()))
            stack.enter_context(patch("main._trigger_dispatch"))
            stack.enter_context(patch("services.project_loops.create_loop_job", create))
            stack.enter_context(
                patch("services.job_provisioning.provision_job_repo", AsyncMock())
            )
            from main import _spawn_loop_job

            await _spawn_loop_job(loop, role="critic", iteration=2, park_until=park)
        assert create.await_args.kwargs["park_until"] == park


class TestSpawnRequiresUnattendedOperationsGrant:
    """Every loop spawn — the start endpoint's first stage, the rotation
    advance, the campaign advance — funnels through ``_spawn_loop_stage``,
    which is why the ``unattended_operations`` re-check lives there. Revoking
    the grant under a running loop must HALT it at the next advance, not let it
    keep spending unattended. knowledge-history/done/unattended_operations_grant.md.
    """

    @staticmethod
    def _db(*, granted: bool, owner: dict | None = None):
        db = AsyncMock()
        db.get_user = AsyncMock(
            return_value=owner if owner is not None else {"id": "u1"}
        )
        db.user_can_run_unattended_operations = AsyncMock(return_value=granted)
        return db

    @pytest.mark.asyncio
    async def test_revoked_grant_halts_the_spawn(self):
        loop = _loop(owner_id="u1", project_id="p1")
        job = AsyncMock()
        db = self._db(granted=False)
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", db))
            stack.enter_context(patch("main._spawn_loop_job", job))
            from main import _spawn_loop_stage

            with pytest.raises(PermissionError) as exc:
                await _spawn_loop_stage(
                    loop, stage="scholar", seq_index=0, base_total=0, remaining=5
                )

        assert "unattended_operations" in str(exc.value)
        job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_held_grant_spawns_normally(self):
        loop = _loop(owner_id="u1", project_id="p1")
        job = AsyncMock(return_value={"id": "j1"})
        db = self._db(granted=True)
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", db))
            stack.enter_context(patch("main._spawn_loop_job", job))
            from main import _spawn_loop_stage

            jobs, total = await _spawn_loop_stage(
                loop, stage="scholar", seq_index=0, base_total=0, remaining=5
            )

        assert total == 1
        assert jobs == [{"id": "j1"}]

    @pytest.mark.asyncio
    async def test_the_grant_is_read_for_the_owner_on_the_loops_project(self):
        """The spawned jobs run as the OWNER, so the owner's grant is the one
        that must still hold — not that of whoever originally clicked start."""
        loop = _loop(owner_id="u7", project_id="p9")
        db = self._db(granted=True, owner={"id": "u7"})
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", db))
            stack.enter_context(patch("main._spawn_loop_job", AsyncMock()))
            from main import _spawn_loop_stage

            await _spawn_loop_stage(
                loop, stage="scholar", seq_index=0, base_total=0, remaining=5
            )

        assert db.get_user.await_args.args == ("u7",)
        user, project_id = db.user_can_run_unattended_operations.await_args.args
        assert user == {"id": "u7"}
        assert project_id == "p9"

    @pytest.mark.asyncio
    async def test_ownerless_loop_is_not_gated(self):
        """A loop with no owner is a system child — there is no principal whose
        grants to resolve, matching ``_enforce_job_create_grants``'s no-op."""
        loop = _loop(project_id="p1")  # no owner_id
        db = self._db(granted=False)
        with ExitStack() as stack:
            stack.enter_context(patch("main.postgres_db", db))
            stack.enter_context(patch("main._spawn_loop_job", AsyncMock()))
            from main import _spawn_loop_stage

            await _spawn_loop_stage(
                loop, stage="scholar", seq_index=0, base_total=0, remaining=5
            )

        db.user_can_run_unattended_operations.assert_not_awaited()


class TestTurnOutcomeReachesRotation:
    """The rotate must be told the TURN outcome, not one member's.

    ``completed_failed`` is the barrier winner's own status. On a fan-out
    turn that is whichever member happened to finish last, which says
    nothing about whether the turn produced anything. Rotation decisions
    need the aggregate — knowledge-base/knowledge/features/better_resavio_restart_status.md §6c.
    """

    @pytest.mark.asyncio
    async def test_all_members_failed_is_reported_to_the_rotate(self):
        job = _job(status="failed", role="critic")
        loop = _loop(current_job_id=job["id"], current_stage_jobs=[job["id"]])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {"error": "critic blew up"}, [])
        kw = rotate.await_args.kwargs
        assert kw["turn_all_failed"] is True
        assert kw["consecutive"] == 1

    @pytest.mark.asyncio
    async def test_a_successful_turn_is_not_reported_as_failed(self):
        job = _job(role="critic")
        loop = _loop(current_job_id=job["id"], current_stage_jobs=[job["id"]])
        db = _advance_db(loop, [job])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(job, {}, [])
        assert rotate.await_args.kwargs["turn_all_failed"] is False

    @pytest.mark.asyncio
    async def test_one_surviving_member_is_not_a_failed_turn(self):
        """Partial success means SOMETHING landed — rotation should advance."""
        ok, bad = _job(role="scholar"), _job(status="failed", role="scholar")
        loop = _loop(current_stage_jobs=[ok["id"], bad["id"]], current_job_id=ok["id"])
        db = _advance_db(loop, [ok, bad])
        rotate = AsyncMock()
        with ExitStack() as stack:
            _advance_patches(stack, db, rotate=rotate)
            from main import _advance_project_loop

            await _advance_project_loop(bad, {"error": "one leg died"}, [])
        kw = rotate.await_args.kwargs
        assert kw["turn_all_failed"] is False
        assert kw["consecutive"] == 0
