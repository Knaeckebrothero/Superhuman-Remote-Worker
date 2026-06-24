"""Tests for orchestrator/security/access.py.

The 3-user fixture (`user_a`, `user_b`, `user_admin`, `fake_db`, ...) is
defined in conftest.py — see the comment block there for the topology.
Every test below exercises the doc's invariants from §"Principles":
owner passes, non-member is denied, admin always passes.
"""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from security import access


# =============================================================================
# Helpers: stub require_approved_user so we don't traverse the full auth path
# =============================================================================
#
# All access.py dependencies call `require_approved_user(request, db)` to
# resolve the caller. Tests inject the desired user via a patch on that
# import inside `security.access`.


def _patch_caller(user: dict):
    """Context manager: make `require_approved_user` return ``user``."""
    return patch.object(access, "require_approved_user", AsyncMock(return_value=user))


# =============================================================================
# user_visible_project_ids
# =============================================================================


class TestUserVisibleProjectIds:
    @pytest.mark.asyncio
    async def test_admin_returns_all_sentinel(self, user_admin, fake_db):
        result = await access.user_visible_project_ids(user_admin, fake_db)
        assert result == "all"
        # Admin bypass means we never even hit the DB
        fake_db.get_projects_for_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_user_returns_their_projects(self, user_a, project_a, fake_db):
        result = await access.user_visible_project_ids(user_a, fake_db)
        assert result == {project_a["id"]}

    @pytest.mark.asyncio
    async def test_user_with_no_projects_returns_empty_set(self, fake_db):
        loner = {
            "id": UUID("99999999-9999-9999-9999-999999999999"),
            "is_admin": False,
            "is_approved": True,
        }
        result = await access.user_visible_project_ids(loner, fake_db)
        assert result == set()


# =============================================================================
# user_visible_jobs_clause
# =============================================================================


class TestUserVisibleJobsClause:
    def test_admin_clause_is_true(self, user_admin):
        clause, params = access.user_visible_jobs_clause(user_admin)
        assert clause == "TRUE"
        assert params == {}

    def test_user_clause_binds_uid_and_empty_projects(self, user_a):
        clause, params = access.user_visible_jobs_clause(user_a)
        assert ":uid" in clause and ":projects" in clause
        assert "jobs.user_id" in clause
        assert "jobs.project_id" in clause
        assert params["uid"] == user_a["id"]
        # Caller is expected to fill in the real project list — helper is sync.
        assert params["projects"] == []

    def test_custom_table_alias_and_params(self, user_a):
        clause, params = access.user_visible_jobs_clause(
            user_a, table_alias="j", user_param="me", projects_param="pids"
        )
        assert "j.user_id = :me" in clause
        assert "j.project_id = ANY(:pids)" in clause
        assert "me" in params and "pids" in params


# =============================================================================
# require_project_member — role hierarchy and admin bypass
# =============================================================================


class TestRequireProjectMember:
    @pytest.mark.asyncio
    async def test_owner_passes_viewer_check(
        self, user_a, project_a, fake_db, fake_request
    ):
        with _patch_caller(user_a):
            user, project = await access.require_project_member(
                fake_request, fake_db, str(project_a["id"])
            )
        assert user is user_a
        assert project is project_a

    @pytest.mark.asyncio
    async def test_owner_passes_owner_check(
        self, user_a, project_a, fake_db, fake_request
    ):
        with _patch_caller(user_a):
            user, project = await access.require_project_member(
                fake_request, fake_db, str(project_a["id"]), min_role="owner"
            )
        assert user is user_a

    @pytest.mark.asyncio
    async def test_non_member_403(self, user_b, project_a, fake_db, fake_request):
        with _patch_caller(user_b):
            with pytest.raises(HTTPException) as exc:
                await access.require_project_member(
                    fake_request, fake_db, str(project_a["id"])
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_project_404(self, user_a, fake_db, fake_request):
        with _patch_caller(user_a):
            with pytest.raises(HTTPException) as exc:
                await access.require_project_member(
                    fake_request,
                    fake_db,
                    "ffffffff-ffff-ffff-ffff-ffffffffffff",
                )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_bypasses_membership_check(
        self, user_admin, project_a, fake_db, fake_request
    ):
        with _patch_caller(user_admin):
            user, project = await access.require_project_member(
                fake_request, fake_db, str(project_a["id"]), min_role="owner"
            )
        assert user is user_admin
        assert project is project_a
        # Admin path short-circuits before consulting the role table.
        fake_db.get_user_role_in_project.assert_not_awaited()


# =============================================================================
# require_project_owner — narrower wrapper, owner role required exactly
# =============================================================================


class TestRequireProjectOwner:
    @pytest.mark.asyncio
    async def test_owner_passes(self, user_a, project_a, fake_db, fake_request):
        with _patch_caller(user_a):
            user, project = await access.require_project_owner(
                fake_request, fake_db, str(project_a["id"])
            )
        assert user is user_a
        assert project is project_a

    @pytest.mark.asyncio
    async def test_non_owner_403(self, user_b, project_a, fake_db, fake_request):
        with _patch_caller(user_b):
            with pytest.raises(HTTPException) as exc:
                await access.require_project_owner(
                    fake_request, fake_db, str(project_a["id"])
                )
        assert exc.value.status_code == 403
        assert "owner" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_admin_bypasses(self, user_admin, project_a, fake_db, fake_request):
        with _patch_caller(user_admin):
            user, project = await access.require_project_owner(
                fake_request, fake_db, str(project_a["id"])
            )
        assert user is user_admin


# =============================================================================
# require_job_access — owner OR project member OR admin
# =============================================================================


class TestRequireJobAccess:
    @pytest.mark.asyncio
    async def test_job_owner_passes(self, user_a, job_a, fake_db, fake_request):
        with _patch_caller(user_a):
            user, job = await access.require_job_access(
                fake_request, fake_db, str(job_a["id"])
            )
        assert user is user_a
        assert job is job_a

    @pytest.mark.asyncio
    async def test_other_user_403(self, user_b, job_a, fake_db, fake_request):
        with _patch_caller(user_b):
            with pytest.raises(HTTPException) as exc:
                await access.require_job_access(fake_request, fake_db, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_bypasses(self, user_admin, job_b, fake_db, fake_request):
        with _patch_caller(user_admin):
            user, job = await access.require_job_access(
                fake_request, fake_db, str(job_b["id"])
            )
        assert user is user_admin
        assert job is job_b

    @pytest.mark.asyncio
    async def test_missing_job_404(self, user_a, fake_db, fake_request):
        with _patch_caller(user_a):
            with pytest.raises(HTTPException) as exc:
                await access.require_job_access(
                    fake_request,
                    fake_db,
                    "deaddead-dead-dead-dead-deaddeaddead",
                )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_project_member_can_access_jobs_in_project(
        self, user_a, job_a, fake_db, fake_request
    ):
        # user_a is owner of project_a, so they already pass via owner-of-job.
        # Simulate a *different* job in project_a owned by user_admin, so
        # user_a's only path through is project membership.
        admin_owned_job = {
            "id": UUID("c1c1c1c1-c1c1-c1c1-c1c1-c1c1c1c1c1c1"),
            "user_id": UUID("33333333-3333-3333-3333-333333333333"),
            "project_id": job_a["project_id"],
            "status": "created",
        }
        original = fake_db.get_job.side_effect

        async def patched(jid):
            if str(jid) == str(admin_owned_job["id"]):
                return admin_owned_job
            return await original(jid)

        fake_db.get_job.side_effect = patched
        with _patch_caller(user_a):
            user, job = await access.require_job_access(
                fake_request, fake_db, str(admin_owned_job["id"])
            )
        assert job["id"] == admin_owned_job["id"]


# =============================================================================
# user_can_access_job — bool variant used by the SSE event filter (F6)
# =============================================================================


class TestUserCanAccessJob:
    @pytest.mark.asyncio
    async def test_admin_always_true(self, user_admin, fake_db):
        # Even with bogus id, admin short-circuits to True without a DB call.
        assert await access.user_can_access_job(user_admin, fake_db, "anything")
        fake_db.get_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_owner_true(self, user_a, job_a, fake_db):
        assert await access.user_can_access_job(user_a, fake_db, str(job_a["id"]))

    @pytest.mark.asyncio
    async def test_other_user_false(self, user_b, job_a, fake_db):
        assert not await access.user_can_access_job(user_b, fake_db, str(job_a["id"]))

    @pytest.mark.asyncio
    async def test_project_member_true(self, user_a, job_a, fake_db):
        """user_a owns project_a; an admin-owned job in project_a is
        still accessible to user_a via the membership path."""
        admin_job = {
            "id": UUID("d1d1d1d1-d1d1-d1d1-d1d1-d1d1d1d1d1d1"),
            "user_id": UUID("33333333-3333-3333-3333-333333333333"),
            "project_id": job_a["project_id"],
            "status": "created",
        }
        original = fake_db.get_job.side_effect

        async def patched(jid):
            if str(jid) == str(admin_job["id"]):
                return admin_job
            return await original(jid)

        fake_db.get_job.side_effect = patched
        assert await access.user_can_access_job(user_a, fake_db, str(admin_job["id"]))

    @pytest.mark.asyncio
    async def test_none_job_id_false_for_non_admin(self, user_a, fake_db):
        """Orphan events (no job_id) fail closed for non-admins.

        This is the SSE-filter use case: `request_decided` events used
        to lack ``job_id`` entirely; F6 added it but old in-flight events
        could still arrive with None. Drop them for non-admins.
        """
        assert not await access.user_can_access_job(user_a, fake_db, None)
        assert not await access.user_can_access_job(user_a, fake_db, "")
        # Admin still passes (separate test above, kept here for clarity)
        # so the full matrix is in one place.

    @pytest.mark.asyncio
    async def test_unknown_job_id_false(self, user_a, fake_db):
        assert not await access.user_can_access_job(
            user_a, fake_db, "ffffffff-ffff-ffff-ffff-ffffffffffff"
        )


# =============================================================================
# user_can_access_ide_entity — accepts job OR thread UUID, returns bool
# =============================================================================


class TestUserCanAccessIdeEntity:
    @pytest.mark.asyncio
    async def test_admin_always_true(self, user_admin, fake_db):
        result = await access.user_can_access_ide_entity(
            user_admin, fake_db, "any-id-at-all"
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_job_owner_true(self, user_a, job_a, fake_db):
        result = await access.user_can_access_ide_entity(
            user_a, fake_db, str(job_a["id"])
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_other_user_jobs_false(self, user_b, job_a, fake_db):
        result = await access.user_can_access_ide_entity(
            user_b, fake_db, str(job_a["id"])
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_thread_owner_true(self, user_a, thread_a, fake_db):
        result = await access.user_can_access_ide_entity(
            user_a, fake_db, str(thread_a["id"])
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_other_user_threads_false(self, user_b, thread_a, fake_db):
        result = await access.user_can_access_ide_entity(
            user_b, fake_db, str(thread_a["id"])
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_unknown_id_false(self, user_a, fake_db):
        result = await access.user_can_access_ide_entity(
            user_a, fake_db, "unknown-but-shaped-like-uuid"
        )
        assert result is False


# =============================================================================
# user_can_access_job_or_thread — citations carry job_id == thread_id for
# sessions; this is the gate behind /api/citations/{id}{,/snapshot,/drift}
# =============================================================================


class TestUserCanAccessJobOrThread:
    @pytest.mark.asyncio
    async def test_admin_always_true(self, user_admin, fake_db):
        assert (
            await access.user_can_access_job_or_thread(
                user_admin, fake_db, "any-id-at-all"
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_job_owner_true(self, user_a, job_a, fake_db):
        assert (
            await access.user_can_access_job_or_thread(
                user_a, fake_db, str(job_a["id"])
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_other_user_job_false(self, user_b, job_a, fake_db):
        assert (
            await access.user_can_access_job_or_thread(
                user_b, fake_db, str(job_a["id"])
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_thread_owner_true(self, user_a, thread_a, fake_db):
        # The session-citation case: job_id is actually a thread id the user owns.
        assert (
            await access.user_can_access_job_or_thread(
                user_a, fake_db, str(thread_a["id"])
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_other_user_thread_false(self, user_b, thread_a, fake_db):
        assert (
            await access.user_can_access_job_or_thread(
                user_b, fake_db, str(thread_a["id"])
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_unknown_id_false(self, user_a, fake_db):
        assert (
            await access.user_can_access_job_or_thread(
                user_a, fake_db, "neither-job-nor-thread"
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_none_entity_false(self, user_a, fake_db):
        # Orphan citation (job_id NULL) → caller passes None → fail closed.
        assert (
            await access.user_can_access_job_or_thread(user_a, fake_db, None) is False
        )


# =============================================================================
# require_sudo_request_authority — owner of related job's project, or admin
# =============================================================================


class TestRequireSudoRequestAuthority:
    @pytest.mark.asyncio
    async def test_unknown_request_404(self, user_a, fake_db, fake_request):
        with patch("services.sudo_gate.sudo_gate") as gate:
            gate.get_request = AsyncMock(return_value=None)
            with _patch_caller(user_a):
                with pytest.raises(HTTPException) as exc:
                    await access.require_sudo_request_authority(
                        fake_request, fake_db, "no-such-request"
                    )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_passes(self, user_admin, job_a, fake_db, fake_request):
        sudo_req = {"id": "req-1", "job_id": str(job_a["id"])}
        with patch("services.sudo_gate.sudo_gate") as gate:
            gate.get_request = AsyncMock(return_value=sudo_req)
            with _patch_caller(user_admin):
                result = await access.require_sudo_request_authority(
                    fake_request, fake_db, "req-1"
                )
        assert result is sudo_req

    @pytest.mark.asyncio
    async def test_project_owner_of_related_job_passes(
        self, user_a, job_a, fake_db, fake_request
    ):
        # user_a is owner of project_a, job_a is in project_a → user_a passes
        # even though they're not the job owner.
        sudo_req = {"id": "req-1", "job_id": str(job_a["id"])}
        with patch("services.sudo_gate.sudo_gate") as gate:
            gate.get_request = AsyncMock(return_value=sudo_req)
            with _patch_caller(user_a):
                result = await access.require_sudo_request_authority(
                    fake_request, fake_db, "req-1"
                )
        assert result is sudo_req

    @pytest.mark.asyncio
    async def test_unrelated_user_403(self, user_b, job_a, fake_db, fake_request):
        sudo_req = {"id": "req-1", "job_id": str(job_a["id"])}
        with patch("services.sudo_gate.sudo_gate") as gate:
            gate.get_request = AsyncMock(return_value=sudo_req)
            with _patch_caller(user_b):
                with pytest.raises(HTTPException) as exc:
                    await access.require_sudo_request_authority(
                        fake_request, fake_db, "req-1"
                    )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_orphan_request_admin_only(self, user_a, fake_db, fake_request):
        """Sudo request with no job_id is admin-only."""
        sudo_req = {"id": "req-1", "job_id": None}
        with patch("services.sudo_gate.sudo_gate") as gate:
            gate.get_request = AsyncMock(return_value=sudo_req)
            with _patch_caller(user_a):
                with pytest.raises(HTTPException) as exc:
                    await access.require_sudo_request_authority(
                        fake_request, fake_db, "req-1"
                    )
        assert exc.value.status_code == 403


# =============================================================================
# apply_mcp_scope — additional WHERE filter on top of user visibility
# =============================================================================


class TestApplyMcpScope:
    def test_no_scopes_is_noop(self, user_a):
        clause, params = access.apply_mcp_scope(user_a)
        assert clause == ""
        assert params == {}

    def test_scope_user_is_noop(self, user_a):
        user_a["scopes"] = ["user"]
        clause, params = access.apply_mcp_scope(user_a)
        assert clause == ""
        assert params == {}

    def test_scope_all_is_noop(self, user_a):
        # 'all' is not a magic admin-bypass at the query level — actual admin
        # status comes from the realm role, not the scope. See doc Q#3.
        user_a["scopes"] = ["all"]
        clause, params = access.apply_mcp_scope(user_a)
        assert clause == ""
        assert params == {}

    def test_scope_project_narrows_to_one_project(self, user_a, project_a):
        user_a["scopes"] = [f"project:{project_a['id']}"]
        clause, params = access.apply_mcp_scope(user_a)
        assert "project_id" in clause
        assert params["scope_project"] == project_a["id"]

    def test_malformed_project_scope_fails_closed(self, user_a):
        user_a["scopes"] = ["project:not-a-uuid"]
        clause, params = access.apply_mcp_scope(user_a)
        # An unsatisfiable bind — caller's outer query returns no rows.
        assert "project_id" in clause
        assert params["scope_project"] is None
