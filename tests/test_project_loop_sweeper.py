"""Tests for the project-loop safety-net sweeper, including torn-advance heal.

The sweeper recovers two wedge shapes (docs/issues/loop_advance_nonatomic_wedges_loop.md):

1. Missed completion hook — loop running, ``current_job_id`` points at a
   terminal job → re-run the advance (original behavior).
2. Torn advance — the advance's claim committed (``current_job_id=NULL``) but
   the write-back was lost → re-point at the newest spawned job, reconcile the
   counters from its spawn-time context stamps, then advance (same tick if the
   job is terminal, via the completion hook otherwise).

The DB is faked at the four methods the sweeper touches; ``advance_fn`` is a
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
    _sweep_parallel_stage,
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
    heal_wins: bool = True,
    stage_heal_wins: bool = True,
    barrier_wins: bool = True,
):
    """Fake of the DB methods the sweeper touches. ``jobs`` newest-first."""
    db = AsyncMock()
    db.list_running_project_loops.return_value = loops
    by_id = {str(j["id"]): j for j in jobs}
    db.get_job.side_effect = lambda job_id: by_id.get(str(job_id))
    db.get_newest_loop_stage.return_value = _newest_stage(jobs)
    db.heal_project_loop_pointer.return_value = heal_wins
    db.heal_project_loop_stage.return_value = stage_heal_wins
    db.claim_project_loop_stage_barrier.return_value = barrier_wins
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
    @pytest.mark.asyncio
    async def test_heals_and_returns_job(self):
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan])
        healed = await _heal_wedged_loop(db, _loop())
        assert healed is orphan
        db.heal_project_loop_pointer.assert_awaited_once_with(
            LOOP_ID,
            orphan["id"],
            seq_index=0,
            total_jobs_run=10,
            remaining_iterations=24,
            min_wedge_age_seconds=HEAL_GRACE_SECONDS,
        )

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
        db.heal_project_loop_pointer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_underivable_context_no_heal(self):
        orphan = _job(10, "scholar")
        orphan["context"] = {}
        db = _db([_loop()], [orphan])
        assert await _heal_wedged_loop(db, _loop()) is None
        db.heal_project_loop_pointer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lost_guard_backs_off(self):
        # Concurrent replica healed first — this one must not advance.
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan], heal_wins=False)
        assert await _heal_wedged_loop(db, _loop()) is None


class TestHealAgeGate:
    """A NULL pointer is only a tear once it's OLD — every healthy advance
    also traverses running+NULL between its claim and its write-back, and
    healing inside that window double-spawns the iteration (the live iter-14
    incident, docs/issues/loop_advance_nonatomic_wedges_loop.md)."""

    @pytest.mark.asyncio
    async def test_fresh_null_pointer_is_deferred_silently(self):
        # updated_at = seconds ago → the claim of an advance in flight.
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=datetime.now(timezone.utc) - timedelta(seconds=5))
        db = _db([loop], [orphan])
        assert await _heal_wedged_loop(db, loop) is None
        # Deferred before touching the DB at all — no stage lookup, no heal.
        db.get_newest_loop_stage.assert_not_awaited()
        db.heal_project_loop_pointer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_null_pointer_heals(self):
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=datetime.now(timezone.utc) - timedelta(hours=12))
        db = _db([loop], [orphan])
        assert await _heal_wedged_loop(db, loop) is orphan

    @pytest.mark.asyncio
    async def test_naive_timestamp_treated_as_utc(self):
        # asyncpg normally returns tz-aware datetimes; a naive one must not
        # crash the comparison and reads as UTC.
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
        # No readable updated_at → attempt the heal; the SQL age guard
        # (DB clock) is authoritative and its verdict is respected.
        orphan = _job(10, "scholar")
        loop = _loop(updated_at=None)
        db = _db([loop], [orphan], heal_wins=False)
        assert await _heal_wedged_loop(db, loop) is None
        db.heal_project_loop_pointer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sweep_tick_skips_fresh_wedge_without_advance(self):
        orphan = _job(10, "scholar")  # completed — would advance if healed
        loop = _loop(updated_at=datetime.now(timezone.utc))
        db = _db([loop], [orphan])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()
        db.heal_project_loop_pointer.assert_not_awaited()


class TestSweepTick:
    @pytest.mark.asyncio
    async def test_normal_terminal_advance_still_works(self):
        # Original behavior: pointer set, job terminal → advance.
        job = _job(9, "developer", status="failed")
        db = _db([_loop(current_job_id=job["id"])], [job])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        advance.assert_awaited_once_with(job, {}, [])
        db.heal_project_loop_pointer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_torn_advance_terminal_orphan_heals_then_advances(self):
        # The incident: wedged loop + completed orphan → heal + advance, one tick.
        orphan = _job(10, "scholar")
        db = _db([_loop()], [orphan])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        db.heal_project_loop_pointer.assert_awaited_once()
        advance.assert_awaited_once_with(orphan, {}, [])

    @pytest.mark.asyncio
    async def test_torn_advance_running_orphan_heals_without_advance(self):
        # Orphan still in flight: fix the pointer now, let the completion
        # hook advance it when it finishes.
        orphan = _job(10, "scholar", status="processing")
        db = _db([_loop()], [orphan])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        db.heal_project_loop_pointer.assert_awaited_once()
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
        db = _db([_loop()], [orphan], heal_wins=False)
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_advance_exception_contained(self):
        job = _job(9, "developer", status="failed")
        db = _db([_loop(current_job_id=job["id"])], [job])
        advance = AsyncMock(side_effect=RuntimeError("boom"))
        assert await _sweep_tick(db, advance) == 0  # logged, not raised

    @pytest.mark.asyncio
    async def test_in_flight_pointer_untouched(self):
        job = _job(9, "developer", status="processing")
        db = _db([_loop(current_job_id=job["id"])], [job])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()
        db.heal_project_loop_pointer.assert_not_awaited()


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


class TestSweepParallelStage:
    """Backstop for a loop with a fan-out stage in flight: act only once every
    member is terminal (a missed barrier), and idempotently (via the barrier)."""

    def _stage_loop(self, member_ids):
        return _loop(
            role_sequence=[list(PARALLEL_ROLES[0]), "critic", "developer"],
            seq_index=0,
            current_job_id=None,
            current_stage_jobs=list(member_ids),
        )

    @pytest.mark.asyncio
    async def test_all_terminal_advances_via_barrier(self):
        a = _job(2, "scholar", seq_index=0, remaining=7)
        b = _job(2, "product-qa", seq_index=0, remaining=7)
        loop = self._stage_loop([a["id"], b["id"]])
        db = _db([loop], [b, a])  # newest-first ordering is irrelevant here
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 1
        # Advances on a member (the barrier claim makes the re-run idempotent).
        advance.assert_awaited_once()
        assert advance.await_args.args[0]["id"] in {a["id"], b["id"]}

    @pytest.mark.asyncio
    async def test_member_still_running_skips(self):
        a = _job(2, "scholar", status="completed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="processing", seq_index=0, remaining=7)
        loop = self._stage_loop([a["id"], b["id"]])
        db = _db([loop], [b, a])
        advance = AsyncMock()
        assert await _sweep_tick(db, advance) == 0
        advance.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_and_completed_members_still_advance(self):
        # Mixed terminal states are still "all terminal" → rotate.
        a = _job(2, "scholar", status="failed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="completed", seq_index=0, remaining=7)
        db = _db([self._stage_loop([a["id"], b["id"]])], [b, a])
        advance = AsyncMock()
        assert (
            await _sweep_parallel_stage(
                db, self._stage_loop([a["id"], b["id"]]), [a["id"], b["id"]], advance
            )
            == 1
        )


class TestHealTornParallelStage:
    """A torn parallel advance lands on the same NULL-pointer / empty-set
    signature as a single-role tear; the heal disambiguates by the newest
    stage's width + member statuses."""

    @pytest.mark.asyncio
    async def test_all_terminal_repoints_at_representative(self):
        # Tear A: last-out barrier drained the set, then crashed before rotate.
        # Both members terminal → re-point current_job_id at one, advance rotates.
        a = _job(2, "scholar", seq_index=0, remaining=7)
        b = _job(2, "product-qa", seq_index=0, remaining=7)
        db = _db([_loop()], [b, a])  # newest-first; both share iteration 2
        healed = await _heal_wedged_loop(db, _loop())
        assert healed is not None and healed["id"] in {a["id"], b["id"]}
        db.heal_project_loop_pointer.assert_awaited_once()
        _, kwargs = db.heal_project_loop_pointer.await_args
        assert kwargs["seq_index"] == 0
        assert kwargs["total_jobs_run"] == 2
        assert kwargs["remaining_iterations"] == 7
        db.heal_project_loop_stage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_some_running_restores_barrier_set(self):
        # Tear B: next stage spawned but write-back lost; members still running.
        # Restore current_stage_jobs (ALL members) so the barrier can fire.
        a = _job(2, "scholar", status="completed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="processing", seq_index=0, remaining=7)
        db = _db([_loop()], [b, a])
        assert await _heal_wedged_loop(db, _loop()) is None
        db.heal_project_loop_stage.assert_awaited_once()
        args, kwargs = db.heal_project_loop_stage.await_args
        assert set(args[1]) == {a["id"], b["id"]}  # full membership restored
        assert kwargs["seq_index"] == 0
        db.heal_project_loop_pointer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restore_lost_race_returns_none(self):
        a = _job(2, "scholar", status="completed", seq_index=0, remaining=7)
        b = _job(2, "product-qa", status="processing", seq_index=0, remaining=7)
        db = _db([_loop()], [b, a], stage_heal_wins=False)
        assert await _heal_wedged_loop(db, _loop()) is None
