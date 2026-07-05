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
    _sweep_tick,
)

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


def _job(iteration: int, role: str, status: str = "completed", **over) -> dict:
    job_id = str(uuid.uuid4())
    base = {
        "id": job_id,
        "status": status,
        "config_name": role,
        "context": {
            "loop_id": LOOP_ID,
            "loop_role": role,
            "loop_iteration": iteration,
        },
    }
    base.update(over)
    return base


def _db(loops: list[dict], jobs: list[dict], *, heal_wins: bool = True):
    """Fake of the four DB methods the sweeper touches. ``jobs`` newest-first."""
    db = AsyncMock()
    db.list_running_project_loops.return_value = loops
    db.list_project_loop_jobs.return_value = [
        {"id": j["id"], "status": j["status"]} for j in jobs
    ]
    by_id = {str(j["id"]): j for j in jobs}
    db.get_job.side_effect = lambda job_id: by_id.get(str(job_id))
    db.heal_project_loop_pointer.return_value = heal_wins
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
        # Deferred before touching the DB at all — no job listing, no heal.
        db.list_project_loop_jobs.assert_not_awaited()
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
