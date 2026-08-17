"""Tests for the project-loop safety-net sweeper, including torn-advance heal.

The sweeper recovers ONE wedge shape (knowledge-base/knowledge/issues/loop_advance_nonatomic_wedges_loop.md):
``current_job_id IS NULL AND current_stage_jobs='[]'`` — both pointer columns
empty, regardless of the in-flight turn's width (width 1 included). That
shape is either a torn advance whose write-back was lost (heal restores the
turn's membership, plus the width-1 display mirror, then advances only if
every member is already terminal) or the transient window every healthy
advance also passes through between its barrier claim and its write-back (the
age gate tells them apart). Separately, a turn already tracked in
``current_stage_jobs`` (any width) is barrier-backstopped: act only once every
member is terminal, idempotently via the atomic barrier claim. A legacy
width-1 row tracked only by the display pointer (``current_job_id`` set,
``current_stage_jobs`` empty — a pre-0063 writer) is adopted into membership
rather than healed or advanced in the same tick.

The DB is faked at the methods the sweeper touches; ``advance_fn`` is a
recorded stub (the advance itself is covered by the orchestrator's own tests).
Counter-derivation invariants come from the start endpoint (iteration 1 spawns
role_sequence[0], seq_index 0) and ``create_project_loop`` (remaining seeded
equal to max_iterations, decremented once per completed advance).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from services.project_loop_sweeper import (
    HEAL_GRACE_SECONDS,
    _derive_loop_counters,
    _heal_wedged_loop,
    _sweep_stage,
    _sweep_tick,
)

PARALLEL_ROLES = [["scholar", "product-qa"], "critic", "developer"]

LOOP_ID = "105a6f98-134c-4077-b7e1-6d08916650d7"
ROLES = ["scholar", "critic", "developer"]


def _loop(**over) -> dict:
    base = {
        "id": LOOP_ID,
        "status": "running",
        "current_job_id": None,
        "role_sequence": list(ROLES),
        "seq_index": 2,  # stale — reflects the previous job, as after a torn advance
        "total_jobs_run": 9,
        "max_iterations": 33,
        "remaining_iterations": 25,
        # Well past the heal grace — a genuine tear, not an in-flight advance.
        "updated_at": datetime.now(timezone.utc)
        - timedelta(seconds=HEAL_GRACE_SECONDS * 2),
    }
    base.update(over)
    return base


def _job(
    iteration: int,
    role: str,
    status: str = "completed",
    *,
    seq_index: int | None = None,
    remaining: int | None = None,
    **over,
) -> dict:
    job_id = str(uuid.uuid4())
    ctx = {
        "loop_id": LOOP_ID,
        "loop_role": role,
        "loop_iteration": iteration,
    }
    # Spawn-time counter stamps (present for jobs spawned post-parallel-stages).
    if seq_index is not None:
        ctx["loop_seq_index"] = seq_index
        ctx["loop_remaining"] = remaining
    base = {
        "id": job_id,
        "status": status,
        "config_name": role,
        "context": ctx,
    }
    base.update(over)
    return base


def _iter_of(j: dict):
    c = j.get("context")
    if isinstance(c, str):
        c = json.loads(c)
    return (c or {}).get("loop_iteration")


def _newest_stage(jobs: list[dict]) -> list[dict]:
    """The newest stage = all jobs sharing the newest job's loop_iteration.

    ``jobs`` is newest-first, so ``jobs[0]`` is the newest; members of a fan-out
    stage share its iteration. Mirrors ``PostgresDB.get_newest_loop_stage``.
    """
    if not jobs:
        return []
    top = _iter_of(jobs[0])
    return [j for j in jobs if _iter_of(j) == top]


def _db(
    loops: list[dict],
    jobs: list[dict],
    *,
    stage_heal_wins: bool = True,
    barrier_wins: bool = True,
    ghost_clear_wins: bool = True,
    adopt_wins: bool = True,
):
    """Fake of the DB methods the sweeper touches. ``jobs`` newest-first."""
    db = AsyncMock()
    db.list_running_project_loops.return_value = loops
    by_id = {str(j["id"]): j for j in jobs}
    db.get_job.side_effect = lambda job_id: by_id.get(str(job_id))
    db.get_newest_loop_stage.return_value = _newest_stage(jobs)
    db.heal_project_loop_stage.return_value = stage_heal_wins
    db.project_loop_members_have_live_completion_command.return_value = False
    db.claim_project_loop_stage_barrier.return_value = barrier_wins
    db.clear_project_loop_ghost_stage.return_value = ghost_clear_wins
    db.adopt_project_loop_pointer_turn.return_value = adopt_wins
    db.get_loop_stage_member_statuses.side_effect = lambda ids: {
        str(i): by_id[str(i)]["status"] for i in ids if str(i) in by_id
    }
    return db


class TestDeriveLoopCounters:
    """The pure reconstruction: (seq_index, total_jobs_run, remaining)."""

    def test_incident_shape_iter10_scholar(self):
        # The live wedge: iter 10 orphan scholar on a 33-iteration loop.
        ctx = {"loop_role": "scholar", "loop_iteration": 10}
        assert _derive_loop_counters(_loop(), ctx) == (0, 10, 24)

    def test_claim_spawn_interrupt_is_identity(self):
        # A→B tear: newest job is the OLD current job; derivation must
        # reproduce the counters the row already has (re-advance is safe).
        ctx = {"loop_role": "developer", "loop_iteration": 9}
        assert _derive_loop_counters(_loop(), ctx) == (2, 9, 25)

    def test_first_iteration(self):
        # Start-endpoint tear (create → spawn → lost update): iteration 1.
        ctx = {"loop_role": "scholar", "loop_iteration": 1}
        assert _derive_loop_counters(_loop(), ctx) == (0, 1, 33)

    def test_deadline_only_loop_keeps_remaining_null(self):
        ctx = {"loop_role": "critic", "loop_iteration": 5}
        loop = _loop(max_iterations=None, remaining_iterations=None)
        assert _derive_loop_counters(loop, ctx) == (1, 5, None)

    def test_remaining_clamped_at_zero(self):
        ctx = {"loop_role": "critic", "loop_iteration": 35}
        assert _derive_loop_counters(_loop(), ctx) == (1, 35, 0)

    def test_role_stamp_wins_over_index(self):
        # Index says scholar (iteration 10) but the stamp says critic —
        # trust the spawn-time stamp.
        ctx = {"loop_role": "critic", "loop_iteration": 10}
        assert _derive_loop_counters(_loop(), ctx) == (1, 10, 24)

    def test_unknown_role_stamp_refuses(self):
        ctx = {"loop_role": "welder", "loop_iteration": 10}
        assert _derive_loop_counters(_loop(), ctx) is None

    def test_missing_or_bad_iteration_refuses(self):
        assert _derive_loop_counters(_loop(), {"loop_role": "scholar"}) is None
        assert (
            _derive_loop_counters(
                _loop(), {"loop_role": "scholar", "loop_iteration": "x"}
            )
            is None
        )
        assert (
            _derive_loop_counters(
                _loop(), {"loop_role": "scholar", "loop_iteration": 0}
            )
            is None
        )

    def test_empty_role_sequence_refuses(self):
        ctx = {"loop_role": "scholar", "loop_iteration": 3}
        assert _derive_loop_counters(_loop(role_sequence=[]), ctx) is None


class TestHealWedgedLoop:
    """Both pointer columns empty → restore the newest stage as membership."""

    @pytest.mark.asyncio
    async def test_width1_terminal_restores_membership_and_returns_job(self):
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan])
        healed = await _heal_wedged_loop(db, _loop())
        assert healed is orphan
        db.heal_project_loop_stage.assert_awaited_once_with(
            LOOP_ID,
            [orphan["id"]],
            current_job_id=orphan["id"],
            seq_index=0,
            total_jobs_run=10,
            remaining_iterations=24,
            min_wedge_age_seconds=HEAL_GRACE_SECONDS,
        )

    @pytest.mark.asyncio
    async def test_width1_running_restores_membership_without_advance(self):
        orphan = _job(10, "scholar", status="processing")
        db = _db([_loop()], [orphan])
        assert await _heal_wedged_loop(db, _loop()) is None
        db.heal_project_loop_stage.assert_awaited_once()
        kwargs = db.heal_project_loop_stage.await_args.kwargs
        assert kwargs["current_job_id"] == orphan["id"]

    @pytest.mark.asyncio
    async def test_fanout_all_terminal_returns_representative(self):
        a = _job(2, "scholar", seq_index=0, remaining=7)
        b = _job(2, "product-qa", seq_index=0, remaining=7)
        db = _db([_loop()], [b, a])
        healed = await _heal_wedged_loop(db, _loop())
        assert healed is not None and healed["id"] in {a["id"], b["id"]}
        args, kwargs = db.heal_project_loop_stage.await_args
        assert set(args[1]) == {a["id"], b["id"]}
        assert kwargs["current_job_id"] is None  # fan-out: no display mirror
        assert kwargs["seq_index"] == 0
        assert kwargs["total_jobs_run"] == 2
        assert kwargs["remaining_iterations"] == 7

    @pytest.mark.asyncio
    async def test_fanout_some_running_restores_and_defers(self):
        a = _job(2, "scholar", status="completed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="processing", seq_index=0, remaining=7)
        db = _db([_loop()], [b, a])
        assert await _heal_wedged_loop(db, _loop()) is None
        args, _ = db.heal_project_loop_stage.await_args
        assert set(args[1]) == {a["id"], b["id"]}  # FULL membership restored

    @pytest.mark.asyncio
    async def test_json_string_context_decoded(self):
        orphan = _job(10, "scholar")
        orphan["context"] = json.dumps(orphan["context"])
        db = _db([_loop()], [orphan])
        assert await _heal_wedged_loop(db, _loop()) is orphan

    @pytest.mark.asyncio
    async def test_no_jobs_no_heal(self):
        db = _db([_loop()], [])
        assert await _heal_wedged_loop(db, _loop()) is None
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_underivable_context_no_heal(self):
        orphan = _job(10, "scholar")
        orphan["context"] = {}
        db = _db([_loop()], [orphan])
        assert await _heal_wedged_loop(db, _loop()) is None
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lost_guard_backs_off(self):
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan], stage_heal_wins=False)
        assert await _heal_wedged_loop(db, _loop()) is None

    @pytest.mark.asyncio
    async def test_live_finalizer_stands_down_before_heal(self):
        orphan = _job(10, "scholar")
        loop = _loop()
        db = _db([loop], [orphan])
        db.project_loop_members_have_live_completion_command.return_value = True

        assert (
            await _heal_wedged_loop(db, loop, completion_commands_enabled=True) is None
        )
        db.project_loop_members_have_live_completion_command.assert_awaited_once_with(
            [orphan["id"]]
        )
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_expired_or_parked_command_does_not_hide_old_heal(self):
        orphan = _job(10, "scholar")
        loop = _loop()
        db = _db([loop], [orphan])
        # The authoritative DB helper returns false for resume_finalizer,
        # park_alert, and alert_only routes. Only stand_down is deference.
        db.project_loop_members_have_live_completion_command.return_value = False

        assert (
            await _heal_wedged_loop(db, loop, completion_commands_enabled=True)
            is orphan
        )
        db.heal_project_loop_stage.assert_awaited_once()


class TestHealAgeGate:
    """Empty pointers are only a tear once they're OLD — every healthy
    advance also traverses the both-cleared state between its barrier claim
    and its write-back (the live iter-14 double-spawn incident)."""

    @pytest.mark.asyncio
    async def test_fresh_wedge_is_deferred_silently(self):
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        db = _db([loop], [orphan])
        assert await _heal_wedged_loop(db, loop) is None
        db.get_newest_loop_stage.assert_not_awaited()
        db.project_loop_members_have_live_completion_command.assert_not_awaited()
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_wedge_heals(self):
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=datetime.now(timezone.utc) - timedelta(hours=12))
        db = _db([loop], [orphan])
        assert await _heal_wedged_loop(db, loop) is orphan

    @pytest.mark.asyncio
    async def test_naive_timestamp_treated_as_utc(self):
        orphan = _job(10, "scholar")
        naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
        fresh = _loop(updated_at=naive_now - timedelta(seconds=5))
        db = _db([fresh], [orphan])
        assert await _heal_wedged_loop(db, fresh) is None
        stale = _loop(updated_at=naive_now - timedelta(hours=12))
        db = _db([stale], [orphan])
        assert await _heal_wedged_loop(db, stale) is orphan

    @pytest.mark.asyncio
    async def test_unknown_age_defers_to_db_guard(self):
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=None)
        db = _db([loop], [orphan], stage_heal_wins=False)
        assert await _heal_wedged_loop(db, loop) is None
        db.heal_project_loop_stage.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sweep_tick_skips_fresh_wedge_without_advance(self):
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=datetime.now(timezone.utc))
        db = _db([loop], [orphan])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()
        db.heal_project_loop_stage.assert_not_awaited()


class TestSweepStage:
    """Backstop for a loop with a turn in flight (any width): act only once
    every member is terminal (a missed barrier), idempotently (via the barrier)."""

    def _stage_loop(self, member_ids, **over):
        return _loop(
            current_job_id=(member_ids[0] if len(member_ids) == 1 else None),
            current_stage_jobs=list(member_ids),
            **over,
        )

    @pytest.mark.asyncio
    async def test_width1_terminal_member_advances(self):
        job = _job(9, "developer", status="failed", seq_index=2, remaining=25)
        db = _db([self._stage_loop([job["id"]])], [job])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        advance.assert_awaited_once_with(job, {}, [])
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_width1_running_member_untouched(self):
        job = _job(9, "developer", status="processing", seq_index=2, remaining=25)
        db = _db([self._stage_loop([job["id"]])], [job])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_finalizer_stands_down_before_member_status_scan(self):
        job = _job(9, "developer", status="completed", seq_index=2, remaining=25)
        loop = self._stage_loop([job["id"]])
        db = _db([loop], [job])
        db.project_loop_members_have_live_completion_command.return_value = True
        advance = AsyncMock()

        assert await _sweep_tick(db, advance, completion_commands_enabled=True) == 0
        db.project_loop_members_have_live_completion_command.assert_awaited_once_with(
            [job["id"]]
        )
        db.get_loop_stage_member_statuses.assert_not_awaited()
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flag_off_keeps_legacy_call_graph_vacuous(self):
        job = _job(9, "developer", status="completed", seq_index=2, remaining=25)
        loop = self._stage_loop([job["id"]])
        db = _db([loop], [job])
        db.project_loop_members_have_live_completion_command.return_value = True
        advance = AsyncMock()

        assert await _sweep_tick(db, advance) == 1
        db.project_loop_members_have_live_completion_command.assert_not_awaited()
        advance.assert_awaited_once_with(job, {}, [])

    @pytest.mark.asyncio
    async def test_fanout_all_terminal_advances_via_barrier(self):
        a = _job(2, "scholar", seq_index=0, remaining=7)
        b = _job(2, "product-qa", seq_index=0, remaining=7)
        db = _db([self._stage_loop([a["id"], b["id"]])], [b, a])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        advance.assert_awaited_once()
        assert advance.await_args.args[0]["id"] in {a["id"], b["id"]}

    @pytest.mark.asyncio
    async def test_fanout_member_still_running_skips(self):
        a = _job(2, "scholar", status="completed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="processing", seq_index=0, remaining=7)
        db = _db([self._stage_loop([a["id"], b["id"]])], [b, a])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_pending_atomic_handoff_reconciles_before_loop_scan(self):
        # Crash point: DB transaction committed its predecessor descriptor but
        # the process died anywhere in the external tail. The general scan is
        # independent of successor baseline/status and even loop status.
        job = _job(10, "critic", status="created", seq_index=1, remaining=4)
        loop = self._stage_loop([job["id"]])
        db = _db([loop], [job])
        advance = AsyncMock()
        reconcile = AsyncMock(return_value=1)

        assert (
            await _sweep_tick(
                db,
                advance,
                completion_commands_enabled=True,
                reconcile_handoff_fn=reconcile,
            )
            == 1
        )
        reconcile.assert_awaited_once_with()
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_flag_off_never_calls_atomic_handoff_reconciler(self):
        job = _job(10, "critic", status="created", seq_index=1, remaining=4)
        loop = self._stage_loop([job["id"]])
        db = _db([loop], [job])
        advance = AsyncMock()
        reconcile = AsyncMock(return_value=True)

        # No flag-on kwarg means the historical call graph: status scan then
        # return, with no command relation or M3 handoff callback touched.
        assert (
            await _sweep_tick(
                db,
                advance,
                reconcile_handoff_fn=reconcile,
            )
            == 0
        )
        reconcile.assert_not_awaited()
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mixed_terminal_states_still_advance(self):
        a = _job(2, "scholar", status="failed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="completed", seq_index=0, remaining=7)
        db = _db([self._stage_loop([a["id"], b["id"]])], [b, a])
        advance = AsyncMock()
        assert (
            await _sweep_stage(
                db, self._stage_loop([a["id"], b["id"]]), [a["id"], b["id"]], advance
            )
            == 1
        )

    @pytest.mark.asyncio
    async def test_ghost_member_treated_terminal_advances_on_survivor(self):
        # A DELETE /api/jobs/{id} row-deleted one member; its id stays listed
        # in current_stage_jobs but no longer resolves. The ghost is treated
        # as terminal (mirrors _advance_loop_member's "failed" default), and
        # the surviving terminal member drives the recovery advance.
        ghost_id = str(uuid.uuid4())
        survivor = _job(9, "developer", status="completed", seq_index=2, remaining=25)
        stage_ids = [ghost_id, survivor["id"]]
        db = _db([self._stage_loop(stage_ids)], [survivor])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        advance.assert_awaited_once_with(survivor, {}, [])
        db.clear_project_loop_ghost_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ghost_member_with_running_survivor_skips(self):
        # Ghost alongside a survivor that's still running: the stage stays
        # open (its own completion hook will fire the barrier) — no advance,
        # and no ghost-clear (a survivor still exists).
        ghost_id = str(uuid.uuid4())
        survivor = _job(9, "developer", status="processing", seq_index=2, remaining=25)
        stage_ids = [ghost_id, survivor["id"]]
        db = _db([self._stage_loop(stage_ids)], [survivor])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()
        db.clear_project_loop_ghost_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_ghost_stage_cleared_for_heal(self):
        # Every listed member has been deleted — the barrier can never fire
        # (its predicate needs an existing member row). Clear the stage so
        # the next tick's heal re-points the loop at its newest surviving
        # stage, instead of wedging forever.
        stage_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        db = _db([self._stage_loop(stage_ids)], [])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        db.clear_project_loop_ghost_stage.assert_awaited_once_with(LOOP_ID, stage_ids)
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_ghost_clear_lost_guard_backs_off(self):
        # A concurrent replica already cleared (or re-pointed) the loop; the
        # guarded UPDATE matches no row. Back off quietly — no advance, no
        # crash.
        stage_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        db = _db([self._stage_loop(stage_ids)], [], ghost_clear_wins=False)
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        db.clear_project_loop_ghost_stage.assert_awaited_once_with(LOOP_ID, stage_ids)
        advance.assert_not_awaited()


class TestSweepTick:
    @pytest.mark.asyncio
    async def test_torn_advance_terminal_orphan_heals_then_advances(self):
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        db.heal_project_loop_stage.assert_awaited_once()
        advance.assert_awaited_once_with(orphan, {}, [])

    @pytest.mark.asyncio
    async def test_torn_advance_running_orphan_heals_without_advance(self):
        orphan = _job(10, "scholar", status="processing")
        db = _db([_loop()], [orphan])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        db.heal_project_loop_stage.assert_awaited_once()
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_pointer_only_row_is_adopted_into_membership(self):
        # Transitional: a pre-0063 writer left a width-1 turn tracked only by
        # current_job_id. The sweeper adopts it (guarded); no advance this tick.
        job = _job(9, "developer", status="completed", seq_index=2, remaining=25)
        db = _db([_loop(current_job_id=job["id"])], [job])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        db.adopt_project_loop_pointer_turn.assert_awaited_once_with(LOOP_ID, job["id"])
        db.update_project_loop.assert_not_awaited()
        advance.assert_not_awaited()
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_legacy_pointer_adopt_defers_to_live_finalizer(self):
        job = _job(9, "developer", status="completed", seq_index=2, remaining=25)
        loop = _loop(current_job_id=job["id"])
        db = _db([loop], [job])
        db.project_loop_members_have_live_completion_command.return_value = True
        advance = AsyncMock()

        assert await _sweep_tick(db, advance, completion_commands_enabled=True) == 0
        db.adopt_project_loop_pointer_turn.assert_not_awaited()
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_adopt_lost_guard_no_advance(self):
        # A concurrent old-replica advance re-pointed the loop between the
        # sweeper's list-read and the adopt write — the guarded UPDATE
        # matches no row. No advance, no crash, no fallback write.
        job = _job(9, "developer", status="completed", seq_index=2, remaining=25)
        db = _db([_loop(current_job_id=job["id"])], [job], adopt_wins=False)
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        db.adopt_project_loop_pointer_turn.assert_awaited_once_with(LOOP_ID, job["id"])
        db.update_project_loop.assert_not_awaited()
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unhealable_loop_is_skipped(self):
        db = _db([_loop()], [])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lost_heal_race_does_not_advance(self):
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan], stage_heal_wins=False)
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_advance_exception_contained(self):
        job = _job(9, "developer", status="failed", seq_index=2, remaining=25)
        loop = _loop(current_job_id=job["id"], current_stage_jobs=[job["id"]])
        db = _db([loop], [job])
        advance = AsyncMock(side_effect=RuntimeError("boom"))
        assert await _sweep_tick(db, advance) == 0  # logged, not raised


class TestDeriveLoopCountersStamps:
    """With spawn-time stamps, derivation reads them directly — robust to
    variable-width parallel stages where the (N-1) % len modulo breaks."""

    def test_stamp_preferred_over_modulo(self):
        # A fan-out loop: stage 0 spawns 2 jobs → job-count 2 at seq_index 0.
        # The legacy modulo would give (2-1) % 3 = 1 (wrong); the stamp wins.
        ctx = {
            "loop_role": "product-qa",
            "loop_iteration": 2,
            "loop_seq_index": 0,
            "loop_remaining": 7,
        }
        loop = _loop(role_sequence=[list(PARALLEL_ROLES[0]), "critic", "developer"])
        assert _derive_loop_counters(loop, ctx) == (0, 2, 7)

    def test_stamp_remaining_none_is_authoritative(self):
        # loop_seq_index present with loop_remaining=None → deadline-only budget,
        # not a missing stamp.
        ctx = {
            "loop_role": "critic",
            "loop_iteration": 3,
            "loop_seq_index": 1,
            "loop_remaining": None,
        }
        assert _derive_loop_counters(_loop(), ctx) == (1, 3, None)

    def test_bad_stamp_falls_back_to_modulo(self):
        # A non-int seq_index stamp is ignored; the legacy path still derives.
        ctx = {
            "loop_role": "scholar",
            "loop_iteration": 10,
            "loop_seq_index": "oops",
        }
        assert _derive_loop_counters(_loop(), ctx) == (0, 10, 24)
