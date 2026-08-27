"""The `unattended_operations` capability grant — the loop half.

Project loops and the commissioned officer are the two surfaces that spawn
jobs with no human clicking anything, so both sit behind one deny-by-default
grant. The officer half rides the config PDP on `officer.enabled` (covered in
tests/test_capability_grants.py) plus a synchronous 403 at the commission
endpoint (tests/test_officer_lifecycle.py); the spawn choke point is covered in
tests/test_loop_unified_advance.py. This file covers the loop ROUTER: which
verbs are gated, which deliberately are not, and the shape of the refusal.

Spec: knowledge-history/done/unattended_operations_grant.md.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

PROJECT_ID = str(uuid.uuid4())
LOOP_ID = str(uuid.uuid4())

CALLER = {"id": "u1", "is_admin": False}


def _loop_row(**over):
    row = {
        "id": LOOP_ID,
        "project_id": PROJECT_ID,
        "status": "running",
        "scheduling": "standard",
        "campaign": None,
    }
    row.update(over)
    return row


@pytest.fixture
def router_auth(monkeypatch):
    """Admit the caller as a project editor; the grant is the only gate left."""
    import routers.project_loops as mod

    monkeypatch.setattr(mod, "require_approved_user", AsyncMock(return_value=CALLER))
    monkeypatch.setattr(mod, "require_project_member", AsyncMock(return_value=None))
    return mod


@pytest.fixture
def db(monkeypatch):
    import main as orch_main

    db = MagicMock()
    db.user_can_run_unattended_operations = AsyncMock(return_value=False)
    db.get_active_project_loop = AsyncMock(return_value=_loop_row())
    db.update_project_loop = AsyncMock(return_value=_loop_row(status="stopped"))
    db.get_officer_thread_for_project = AsyncMock(return_value=None)
    monkeypatch.setattr(orch_main, "postgres_db", db)
    return db


class TestGatedVerbs:
    """Start, resume and convert-to-officer put unattended work in motion."""

    @pytest.mark.asyncio
    async def test_start_403s_without_the_grant(self, router_auth, db):
        from routers.project_loops import ProjectLoopStart, start_project_loop

        with pytest.raises(HTTPException) as exc:
            await start_project_loop(
                MagicMock(), PROJECT_ID, ProjectLoopStart(max_iterations=5)
            )

        assert exc.value.status_code == 403
        assert "unattended_operations" in str(exc.value.detail)

    @pytest.mark.asyncio
    async def test_resume_403s_without_the_grant(self, router_auth, db):
        """Resume re-kicks the rotation, so it is a start, not a control action."""
        from routers.project_loops import resume_project_loop

        with pytest.raises(HTTPException) as exc:
            await resume_project_loop(MagicMock(), PROJECT_ID)

        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_convert_to_officer_403s_without_the_grant(self, router_auth, db):
        from routers.project_loops import (
            ProjectLoopScheduling,
            convert_project_loop_scheduling,
        )

        with pytest.raises(HTTPException) as exc:
            await convert_project_loop_scheduling(
                MagicMock(), PROJECT_ID, ProjectLoopScheduling(scheduling="officer")
            )

        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_the_grant_is_resolved_against_this_project(self, router_auth, db):
        """Project scope is the axis an operator most wants; dropping the id
        would silently reduce the key to a user-only capability."""
        from routers.project_loops import ProjectLoopStart, start_project_loop

        with pytest.raises(HTTPException):
            await start_project_loop(
                MagicMock(), PROJECT_ID, ProjectLoopStart(max_iterations=5)
            )

        caller, project_id = db.user_can_run_unattended_operations.await_args.args
        assert caller == CALLER
        assert project_id == PROJECT_ID

    @pytest.mark.asyncio
    async def test_start_refuses_before_validating_the_body(self, router_auth, db):
        """The gate runs ahead of the budget/role/workspace validation, so a
        user without the grant gets the reason that actually applies to them
        rather than a 400 about iteration budgets."""
        from routers.project_loops import ProjectLoopStart, start_project_loop

        with pytest.raises(HTTPException) as exc:
            # No max_iterations and no run_until — normally a 400.
            await start_project_loop(MagicMock(), PROJECT_ID, ProjectLoopStart())

        assert exc.value.status_code == 403


class TestHaltingIsNeverGated:
    """Nobody may be locked out of STOPPING work that is already running. The
    fail-closed direction is "no new work", not "no control" — a grant revoked
    mid-run must not strand a live loop with no way to halt it from the UI.
    """

    @pytest.mark.asyncio
    async def test_pause_works_without_the_grant(self, router_auth, db):
        from routers.project_loops import pause_project_loop

        db.update_project_loop = AsyncMock(return_value=_loop_row(status="paused"))

        out = await pause_project_loop(MagicMock(), PROJECT_ID)

        assert out["status"] == "paused"
        db.user_can_run_unattended_operations.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stop_works_without_the_grant(self, router_auth, db):
        from routers.project_loops import stop_project_loop

        out = await stop_project_loop(MagicMock(), PROJECT_ID)

        assert out["status"] == "stopped"
        db.user_can_run_unattended_operations.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reading_the_loop_works_without_the_grant(self, router_auth, db):
        from routers.project_loops import get_project_loop

        out = await get_project_loop(MagicMock(), PROJECT_ID)

        assert out["id"] == LOOP_ID
        db.user_can_run_unattended_operations.assert_not_awaited()


class TestAdminBypass:
    @pytest.mark.asyncio
    async def test_admins_are_not_stopped_by_the_gate(self, router_auth, db):
        """Admins short-circuit inside the DB helper, so the router gate must
        consult it rather than reading a resolved grants dict itself."""
        from routers.project_loops import _require_unattended_operations

        db.user_can_run_unattended_operations = AsyncMock(return_value=True)

        await _require_unattended_operations(
            db, {"id": "admin", "is_admin": True}, PROJECT_ID
        )


# ---------------------------------------------------------------------------
# The capability read itself. Modelled on tests/test_merged_pr_completion_grant.py,
# whose helper this one mirrors (including the project_id passthrough).
# ---------------------------------------------------------------------------

EMPTY_SCOPES = {"user": [], "project": [], "global": []}


def _db_with_grant_rows(scoped):
    """PostgresDB with no pool — only list_grants_for_scopes is exercised."""
    from database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db.list_grants_for_scopes = AsyncMock(return_value=scoped)
    return db


class TestUserCanRunUnattendedOperations:
    @pytest.mark.asyncio
    async def test_admin_short_circuits_without_grant_read(self):
        db = _db_with_grant_rows(EMPTY_SCOPES)
        assert (
            await db.user_can_run_unattended_operations(
                {"id": "u1", "is_admin": True}, PROJECT_ID
            )
            is True
        )
        db.list_grants_for_scopes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_rows_denies_by_default(self):
        db = _db_with_grant_rows(EMPTY_SCOPES)
        assert (
            await db.user_can_run_unattended_operations(
                {"id": "u1", "is_admin": False}, PROJECT_ID
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_user_scope_grant_allows(self):
        db = _db_with_grant_rows(
            {
                "user": [{"key": "unattended_operations", "value_json": True}],
                "project": [],
                "global": [],
            }
        )
        assert (
            await db.user_can_run_unattended_operations(
                {"id": "u1", "is_admin": False}, PROJECT_ID
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_project_scope_grant_allows(self):
        """ "This team may run loops, that one may not" — the axis the grant
        exists for, and the reason project_id must reach the read."""
        db = _db_with_grant_rows(
            {
                "user": [],
                "project": [{"key": "unattended_operations", "value_json": True}],
                "global": [],
            }
        )
        assert (
            await db.user_can_run_unattended_operations(
                {"id": "u1", "is_admin": False}, PROJECT_ID
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_a_user_grant_cannot_widen_past_a_global_denial(self):
        """Restrict-only: the catalog's `meet` clamps to the most restrictive
        scope that set the key, so a global off-switch holds against a
        per-user grant left behind from before."""
        db = _db_with_grant_rows(
            {
                "user": [{"key": "unattended_operations", "value_json": True}],
                "project": [],
                "global": [{"key": "unattended_operations", "value_json": False}],
            }
        )
        assert (
            await db.user_can_run_unattended_operations(
                {"id": "u1", "is_admin": False}, PROJECT_ID
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_the_projects_scope_is_actually_queried(self):
        db = _db_with_grant_rows(EMPTY_SCOPES)
        await db.user_can_run_unattended_operations(
            {"id": "u1", "is_admin": False}, PROJECT_ID
        )
        assert db.list_grants_for_scopes.await_args.kwargs["project_ids"] == [
            PROJECT_ID
        ]

    @pytest.mark.asyncio
    async def test_no_project_still_reads_user_and_global_scopes(self):
        db = _db_with_grant_rows(EMPTY_SCOPES)
        await db.user_can_run_unattended_operations(
            {"id": "u1", "is_admin": False}, None
        )
        assert db.list_grants_for_scopes.await_args.kwargs["project_ids"] == []

    @pytest.mark.asyncio
    async def test_grant_read_failure_fails_closed(self):
        from database.postgres import PostgresDB

        db = PostgresDB.__new__(PostgresDB)
        db.list_grants_for_scopes = AsyncMock(side_effect=RuntimeError("db down"))
        assert (
            await db.user_can_run_unattended_operations(
                {"id": "u1", "is_admin": False}, PROJECT_ID
            )
            is False
        )
