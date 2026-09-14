"""S8 — scheduling='officer' for project loops (knowledge-base/knowledge/features/centurion.md §7).

Covers: migration 0075 CHECK shape; router vocabulary; the advance-path
officer branch (clears pointers, wakes the officer, never rotates); the
sweeper's heal guard (empty pointers are the officer loop's steady state,
not a torn advance); and the guarded one-way conversion writer.
Live wake delivery is k3d-smoke territory, as with the substrate suite.
"""

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services import project_loop_advance
from orchestrator.services.project_loop_spawn import ProjectLoopDependencies
from pydantic import ValidationError

_REPO = Path(__file__).resolve().parents[1]
_MIGRATION = (
    _REPO
    / "src"
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
    / "0075_project_loop_officer_scheduling.sql"
)

LOOP_ID = str(uuid.uuid4())
PROJECT_ID = str(uuid.uuid4())
JOB_ID = str(uuid.uuid4())


# =============================================================================
# Migration 0075
# =============================================================================


class TestMigration:
    def test_scheduling_check_admits_officer(self):
        sql = _MIGRATION.read_text()
        assert "project_loop_scheduling_known" in sql
        assert "'officer'" in sql
        assert "'standard'" in sql and "'campaign'" in sql

    def test_budget_check_relaxed_for_officer(self):
        sql = _MIGRATION.read_text()
        assert "project_loop_has_budget" in sql
        assert "scheduling = 'officer'" in sql


# =============================================================================
# Router vocabulary
# =============================================================================


class TestRouterModels:
    def test_start_accepts_officer(self):
        from orchestrator.routers.project_loops import ProjectLoopStart

        body = ProjectLoopStart(scheduling="officer")
        assert body.scheduling == "officer"

    def test_start_rejects_unknown_mode(self):
        from orchestrator.routers.project_loops import ProjectLoopStart

        with pytest.raises(ValidationError):
            ProjectLoopStart(scheduling="centurion")

    def test_conversion_body_is_officer_only(self):
        from orchestrator.routers.project_loops import ProjectLoopScheduling

        assert ProjectLoopScheduling(scheduling="officer").scheduling == "officer"
        with pytest.raises(ValidationError):
            ProjectLoopScheduling(scheduling="standard")


# =============================================================================
# Advance-path officer branch
# =============================================================================


def _loop_row(scheduling="officer", **over):
    row = {
        "id": LOOP_ID,
        "project_id": PROJECT_ID,
        "status": "running",
        "scheduling": scheduling,
        "current_stage_jobs": [JOB_ID],
        "current_job_id": JOB_ID,
        "seq_index": 3,
        "total_jobs_run": 7,
        "remaining_iterations": 5,
        "consecutive_failures": 0,
        "max_consecutive_failures": 3,
        "campaign": None,
    }
    row.update(over)
    return row


def _job_row(status="completed"):
    return {"id": JOB_ID, "status": status, "project_id": PROJECT_ID}


def _deps(**over) -> ProjectLoopDependencies:
    """The loop engine's one dependency object, built from mocks."""
    fields = dict(
        store=AsyncMock(),
        vector_store=None,
        notifier=AsyncMock(),
        gitea_client=MagicMock(),
        main_cloud_router=MagicMock(),
        trigger_dispatch=MagicMock(),
        kick_officer_event_drain=MagicMock(),
        enforce_dispatch_grants=AsyncMock(),
        reindex_project_kb=AsyncMock(),
        completion_commands_enabled=lambda: False,
        completion_sweep_router=MagicMock(),
    )
    fields.update(over)
    return ProjectLoopDependencies(**fields)


@pytest.fixture
def patched_engine(monkeypatch):
    """Stub the advance's own collaborators and hand back its dependency object.

    Every one of these names is resolved through
    ``orchestrator.services.project_loop_advance``'s module namespace, so the
    patch has to land there; ``kick_officer_event_drain`` is a dependency
    field instead, which is why it arrives on ``deps``.
    """
    db = SimpleNamespace()
    db.claim_project_loop_stage_barrier = AsyncMock(return_value=True)
    db.get_loop_stage_member_statuses = AsyncMock(return_value={JOB_ID: "completed"})
    db.update_project_loop = AsyncMock(return_value=_loop_row())
    monkeypatch.setattr(project_loop_advance, "record_loop_job_outcome", AsyncMock())
    monkeypatch.setattr(project_loop_advance, "notify_loop_user_questions", AsyncMock())
    monkeypatch.setattr(
        project_loop_advance, "notify_officer", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        project_loop_advance, "loop_cooldown_park_until", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(project_loop_advance, "rotate_loop_to_next_stage", AsyncMock())
    return project_loop_advance, db, _deps(store=db)


class TestOfficerAdvanceBranch:
    @pytest.mark.asyncio
    async def test_officer_loop_wakes_instead_of_rotating(self, patched_engine):
        adv, db, deps = patched_engine
        await adv.advance_loop_member(
            _job_row(), {}, [], loop=_loop_row(), ctx={}, dependencies=deps
        )
        # Pointers cleared + failure bookkeeping, no stop/rotate/park.
        db.update_project_loop.assert_awaited_once()
        kwargs = db.update_project_loop.await_args.kwargs
        assert kwargs["current_stage_jobs"] == []
        assert kwargs["current_job_id"] is None
        adv.rotate_loop_to_next_stage.assert_not_awaited()
        adv.loop_cooldown_park_until.assert_not_awaited()
        # Exactly one officer wake, keyed on the turn.
        adv.notify_officer.assert_awaited_once()
        args, kwargs = adv.notify_officer.await_args
        assert args[1] == PROJECT_ID
        assert kwargs["source"] == "loop"
        assert kwargs["dedup_key"] == f"{LOOP_ID[:8]}:3"
        deps.kick_officer_event_drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_iterations_never_decrement(self, patched_engine):
        adv, db, deps = patched_engine
        await adv.advance_loop_member(
            _job_row(),
            {},
            [],
            loop=_loop_row(remaining_iterations=1),
            ctx={},
            dependencies=deps,
        )
        kwargs = db.update_project_loop.await_args.kwargs
        assert "remaining_iterations" not in kwargs
        assert "status" not in kwargs  # never stopped by the branch

    @pytest.mark.asyncio
    async def test_turn_failure_bookkeeping_survives(self, patched_engine):
        adv, db, deps = patched_engine
        db.get_loop_stage_member_statuses = AsyncMock(return_value={JOB_ID: "failed"})
        await adv.advance_loop_member(
            _job_row(status="failed"),
            {"error": "boom"},
            [],
            loop=_loop_row(consecutive_failures=1),
            ctx={},
            dependencies=deps,
        )
        kwargs = db.update_project_loop.await_args.kwargs
        assert kwargs["consecutive_failures"] == 2
        assert kwargs["last_error"] == "boom"
        # A failing turn still NEVER stops an officer loop — judgment does.
        assert "status" not in kwargs
        adv.notify_officer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_barrier_loss_means_no_wake(self, patched_engine):
        adv, db, deps = patched_engine
        db.claim_project_loop_stage_barrier = AsyncMock(return_value=False)
        await adv.advance_loop_member(
            _job_row(), {}, [], loop=_loop_row(), ctx={}, dependencies=deps
        )
        adv.notify_officer.assert_not_awaited()
        db.update_project_loop.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_standard_loop_still_rotates(self, patched_engine):
        adv, db, deps = patched_engine
        monkey_loop = _loop_row(scheduling="standard")
        await adv.advance_loop_member(
            _job_row(), {}, [], loop=monkey_loop, ctx={}, dependencies=deps
        )
        adv.rotate_loop_to_next_stage.assert_awaited_once()
        adv.notify_officer.assert_not_awaited()


# =============================================================================
# Sweeper heal guard
# =============================================================================


class TestSweeperGuard:
    @pytest.mark.asyncio
    async def test_officer_loop_empty_pointers_not_healed(self, monkeypatch):
        from orchestrator.services import project_loop_sweeper as sweeper

        db = SimpleNamespace()
        db.list_running_project_loops = AsyncMock(
            return_value=[_loop_row(current_stage_jobs=[], current_job_id=None)]
        )
        heal = AsyncMock()
        monkeypatch.setattr(sweeper, "_heal_wedged_loop", heal)
        db.adopt_project_loop_pointer_turn = AsyncMock()

        recovered = await sweeper._sweep_tick(db, AsyncMock())
        assert recovered == 0
        heal.assert_not_awaited()
        db.adopt_project_loop_pointer_turn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_officer_loop_inflight_turn_still_swept(self, monkeypatch):
        from orchestrator.services import project_loop_sweeper as sweeper

        stage_sweep = AsyncMock(return_value=1)
        monkeypatch.setattr(sweeper, "_sweep_stage", stage_sweep)
        db = SimpleNamespace()
        db.list_running_project_loops = AsyncMock(return_value=[_loop_row()])

        recovered = await sweeper._sweep_tick(db, AsyncMock())
        assert recovered == 1
        stage_sweep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_standard_loop_heal_still_runs(self, monkeypatch):
        from orchestrator.services import project_loop_sweeper as sweeper

        heal = AsyncMock(return_value=None)
        monkeypatch.setattr(sweeper, "_heal_wedged_loop", heal)
        db = SimpleNamespace()
        db.list_running_project_loops = AsyncMock(
            return_value=[
                _loop_row(
                    scheduling="standard",
                    current_stage_jobs=[],
                    current_job_id=None,
                )
            ]
        )

        await sweeper._sweep_tick(db, AsyncMock())
        heal.assert_awaited_once()


# =============================================================================
# Conversion writer
# =============================================================================


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


class TestConversionWriter:
    @pytest.mark.asyncio
    async def test_guards_live_in_where_clause(self):
        from orchestrator.database.postgres import PostgresDB

        captured = {}

        async def fetchrow(sql, *params):
            captured["sql"] = sql
            return {
                "id": uuid.UUID(LOOP_ID),
                "scheduling": "officer",
                "role_sequence": '["scholar"]',
                "current_stage_jobs": "[]",
                "campaign_history": "[]",
            }

        conn = SimpleNamespace(fetchrow=fetchrow)
        db = PostgresDB.__new__(PostgresDB)
        db.acquire = lambda: _FakeAcquire(conn)

        row = await db.convert_project_loop_to_officer(LOOP_ID)
        assert row["scheduling"] == "officer"
        assert row["role_sequence"] == ["scholar"]  # jsonb decoded
        sql = captured["sql"]
        assert "scheduling = 'officer'" in sql
        assert "status IN ('running', 'paused')" in sql
        assert "scheduling <> 'officer'" in sql
        assert "campaign IS NULL" in sql

    @pytest.mark.asyncio
    async def test_guard_miss_returns_none(self):
        from orchestrator.database.postgres import PostgresDB

        async def fetchrow(sql, *params):
            return None

        conn = SimpleNamespace(fetchrow=fetchrow)
        db = PostgresDB.__new__(PostgresDB)
        db.acquire = lambda: _FakeAcquire(conn)
        assert await db.convert_project_loop_to_officer(LOOP_ID) is None
