"""G2 — multi-tenancy gates on the projects family.

Covers 9 reads + 6 mutations under ``/api/projects`` and
``/api/projects/{id}/...``:

Reads (gate: ``require_project_member`` at viewer):
    GET    /api/projects                          (list_projects)
    GET    /api/projects/{id}                     (get_project)
    GET    /api/projects/{id}/members
    GET    /api/projects/{id}/contacts
    GET    /api/projects/{id}/memory/stats
    GET    /api/projects/{id}/experts
    GET    /api/projects/{id}/experts/{name}
    GET    /api/projects/{id}/repositories
    GET    /api/projects/{id}/jobs

Mutations:
    POST   /api/projects/{id}/contacts            (editor)
    DELETE /api/projects/{id}/contacts/{id}       (editor)
    POST   /api/projects/{id}/repositories        (owner)
    PATCH  /api/projects/{id}/repositories/{id}   (owner)
    DELETE /api/projects/{id}/repositories/{id}   (owner)
    POST   /api/projects/{id}/jobs                (editor)

``list_projects`` semantics:
    * Admin sees full list (or `?user_id=` cross-user)
    * Non-admin auto-restricted to caller's memberships
    * Non-admin `?user_id=<other>` → 403
    * MCP `project:<uuid>` scope filters the result post-fetch
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException


# =============================================================================
# Patch helpers
# =============================================================================


def _patch_caller_and_db(user: dict, db):
    """Stack the patches every endpoint test needs."""
    stack = ExitStack()
    stack.enter_context(
        patch("main.require_approved_user", AsyncMock(return_value=user))
    )
    stack.enter_context(
        patch(
            "security.access.require_approved_user",
            AsyncMock(return_value=user),
        )
    )
    stack.enter_context(patch("main.postgres_db", db))
    return stack


def _scoped(user: dict, scope: str) -> dict:
    out = dict(user)
    out["scopes"] = [scope]
    out["auth_method"] = "mcp"
    return out


def _set_role(db, project_id, user_id, role):
    """Override ``get_user_role_in_project`` to assign a specific role."""
    pid = UUID(str(project_id))
    uid = UUID(str(user_id))

    async def lookup(p, u):
        if UUID(str(p)) == pid and UUID(str(u)) == uid:
            return role
        return None

    db.get_user_role_in_project = AsyncMock(side_effect=lookup)


# Fake acquire() for the admin list_projects path.
def _patch_admin_list_fetch(projects):
    """Patch `postgres_db.acquire()` to return rows for the admin path."""
    conn = MagicMock()
    conn.fetch = AsyncMock(return_value=projects)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


# =============================================================================
# list_projects — visibility model
# =============================================================================


class TestListProjects:
    @pytest.mark.asyncio
    async def test_non_admin_uses_get_projects_for_user(
        self, user_a, fake_db, fake_request
    ):
        from main import list_projects

        with _patch_caller_and_db(user_a, fake_db):
            result = await list_projects(fake_request, user_id=None)

        fake_db.get_projects_for_user.assert_awaited_with(str(user_a["id"]))
        assert len(result) == 1  # user_a is owner of project_a only

    @pytest.mark.asyncio
    async def test_non_admin_cross_user_query_403(
        self, user_a, user_b, fake_db, fake_request
    ):
        from main import list_projects

        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await list_projects(fake_request, user_id=str(user_b["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_non_admin_self_query_allowed(self, user_a, fake_db, fake_request):
        from main import list_projects

        with _patch_caller_and_db(user_a, fake_db):
            result = await list_projects(fake_request, user_id=str(user_a["id"]))
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_admin_no_user_id_uses_admin_view(
        self, user_admin, project_a, project_b, fake_db, fake_request
    ):
        from main import list_projects

        fake_db.acquire = MagicMock(
            return_value=_patch_admin_list_fetch([project_a, project_b])
        )
        with _patch_caller_and_db(user_admin, fake_db):
            result = await list_projects(fake_request, user_id=None)
        assert len(result) == 2
        # The non-admin helper must NOT be called for admin
        fake_db.get_projects_for_user.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_admin_cross_user_query_uses_helper(
        self, user_admin, user_a, fake_db, fake_request
    ):
        from main import list_projects

        with _patch_caller_and_db(user_admin, fake_db):
            await list_projects(fake_request, user_id=str(user_a["id"]))
        fake_db.get_projects_for_user.assert_awaited_with(str(user_a["id"]))

    @pytest.mark.asyncio
    async def test_mcp_project_scope_narrows_admin(
        self, user_admin, project_a, project_b, fake_db, fake_request
    ):
        from main import list_projects

        scoped = _scoped(user_admin, f"project:{project_a['id']}")
        fake_db.acquire = MagicMock(
            return_value=_patch_admin_list_fetch([project_a, project_b])
        )
        with _patch_caller_and_db(scoped, fake_db):
            result = await list_projects(fake_request, user_id=None)
        # Only project_a should remain after scope filter.
        assert len(result) == 1
        assert result[0]["id"] == project_a["id"]

    @pytest.mark.asyncio
    async def test_mcp_project_scope_narrows_non_admin(
        self, user_a, project_a, project_b, fake_db, fake_request
    ):
        from main import list_projects

        scoped = _scoped(user_a, f"project:{project_b['id']}")
        with _patch_caller_and_db(scoped, fake_db):
            result = await list_projects(fake_request, user_id=None)
        # user_a's memberships filtered against project_b → empty.
        assert result == []

    @pytest.mark.asyncio
    async def test_unauthenticated_baseline(self, fake_db, fake_request):
        from main import list_projects

        with (
            patch(
                "main.require_approved_user",
                AsyncMock(side_effect=HTTPException(status_code=401)),
            ),
            patch("main.postgres_db", fake_db),
        ):
            with pytest.raises(HTTPException) as exc:
                await list_projects(fake_request, user_id=None)
        assert exc.value.status_code == 401


# =============================================================================
# get_project — viewer-minimum gate, no over-fetch
# =============================================================================


class TestGetProject:
    @pytest.mark.asyncio
    async def test_owner_passes(self, user_a, project_a, fake_db, fake_request):
        from main import get_project

        with (
            _patch_caller_and_db(user_a, fake_db),
            patch(
                "main._ensure_project_cloud_resources",
                AsyncMock(side_effect=lambda p: p),
            ),
            patch("main.main_cloud_router") as router,
        ):
            router.for_project.return_value.is_initialized = False
            result = await get_project(fake_request, str(project_a["id"]))
        assert result["id"] == project_a["id"]

    @pytest.mark.asyncio
    async def test_cross_user_403(self, user_b, project_a, fake_db, fake_request):
        from main import get_project

        # _ensure_project_cloud_resources must NOT fire — gate first.
        sentinel = AsyncMock(side_effect=AssertionError("side effect ran"))
        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main._ensure_project_cloud_resources", sentinel),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_project(fake_request, str(project_a["id"]))
        assert exc.value.status_code == 403
        sentinel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_404(self, user_a, fake_db, fake_request):
        from main import get_project

        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_project(fake_request, "00000000-0000-0000-0000-000000000999")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_bypass(self, user_admin, project_a, fake_db, fake_request):
        from main import get_project

        with (
            _patch_caller_and_db(user_admin, fake_db),
            patch(
                "main._ensure_project_cloud_resources",
                AsyncMock(side_effect=lambda p: p),
            ),
            patch("main.main_cloud_router") as router,
        ):
            router.for_project.return_value.is_initialized = False
            result = await get_project(fake_request, str(project_a["id"]))
        assert result["id"] == project_a["id"]

    @pytest.mark.asyncio
    async def test_viewer_passes(self, user_b, project_a, fake_db, fake_request):
        """user_b granted viewer on project_a → can read it."""
        from main import get_project

        _set_role(fake_db, project_a["id"], user_b["id"], "viewer")
        with (
            _patch_caller_and_db(user_b, fake_db),
            patch(
                "main._ensure_project_cloud_resources",
                AsyncMock(side_effect=lambda p: p),
            ),
            patch("main.main_cloud_router") as router,
        ):
            router.for_project.return_value.is_initialized = False
            result = await get_project(fake_request, str(project_a["id"]))
        assert result["id"] == project_a["id"]


# =============================================================================
# Read endpoints — gate fires before downstream service
# =============================================================================


def _explode(name: str):
    return MagicMock(side_effect=AssertionError(f"{name} called past the gate"))


class TestProjectReadGates:
    @pytest.mark.asyncio
    async def test_list_members_blocked_cross_user(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import list_project_members

        fake_db.get_project_members = AsyncMock(
            side_effect=AssertionError("get_project_members called past the gate")
        )
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await list_project_members(fake_request, str(project_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_contacts_blocked_cross_user(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import list_external_contacts

        fake_db.get_external_contacts = AsyncMock(
            side_effect=AssertionError("get_external_contacts called past the gate")
        )
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await list_external_contacts(fake_request, str(project_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_memory_stats_blocked_cross_user(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import get_project_memory_stats

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.vector_db", _explode("vector_db")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_project_memory_stats(fake_request, str(project_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_experts_blocked_cross_user(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import list_project_experts

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.gitea_client", _explode("gitea_client")),
        ):
            with pytest.raises(HTTPException) as exc:
                await list_project_experts(fake_request, str(project_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_expert_blocked_cross_user(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import get_project_expert

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.gitea_client", _explode("gitea_client")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_project_expert(fake_request, str(project_a["id"]), "scholar")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_repositories_blocked_cross_user(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import list_project_repositories

        fake_db.get_project_repositories = AsyncMock(
            side_effect=AssertionError("get_project_repositories called past the gate")
        )
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await list_project_repositories(
                    fake_request, str(project_a["id"]), role=None
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_project_jobs_blocked_cross_user(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import list_project_jobs

        # acquire shouldn't be reached
        fake_db.acquire = MagicMock(
            side_effect=AssertionError("acquire called past the gate")
        )
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await list_project_jobs(
                    fake_request, str(project_a["id"]), status=None, limit=100
                )
        assert exc.value.status_code == 403


# =============================================================================
# Read endpoints — happy paths (owner / viewer / admin reach the service)
# =============================================================================


class TestProjectReadHappyPaths:
    @pytest.mark.asyncio
    async def test_list_members_owner_reaches_db(
        self, user_a, project_a, fake_db, fake_request
    ):
        from main import list_project_members

        fake_db.get_project_members = AsyncMock(
            return_value=[{"user_id": str(user_a["id"]), "role": "owner"}]
        )
        with _patch_caller_and_db(user_a, fake_db):
            result = await list_project_members(fake_request, str(project_a["id"]))
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_list_members_admin_bypass(
        self, user_admin, project_a, fake_db, fake_request
    ):
        from main import list_project_members

        fake_db.get_project_members = AsyncMock(return_value=[])
        with _patch_caller_and_db(user_admin, fake_db):
            await list_project_members(fake_request, str(project_a["id"]))
        fake_db.get_project_members.assert_awaited_once()


# =============================================================================
# Mutations — role enforcement (editor vs owner)
# =============================================================================


class TestProjectMutationRoles:
    @pytest.mark.asyncio
    async def test_add_contact_viewer_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import ExternalContactCreate, add_external_contact

        _set_role(fake_db, project_a["id"], user_b["id"], "viewer")
        fake_db.add_external_contact = AsyncMock(
            side_effect=AssertionError("add_external_contact called past the gate")
        )
        body = ExternalContactCreate(display_name="X", email="x@example.test")
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await add_external_contact(str(project_a["id"]), body, fake_request)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_add_contact_editor_passes(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import ExternalContactCreate, add_external_contact

        _set_role(fake_db, project_a["id"], user_b["id"], "editor")
        fake_db.add_external_contact = AsyncMock(
            return_value={
                "id": "00000000-0000-0000-0000-000000000abc",
                "display_name": "X",
                "email": "x@example.test",
                "created_at": None,
            }
        )
        body = ExternalContactCreate(display_name="X", email="x@example.test")
        with _patch_caller_and_db(user_b, fake_db):
            result = await add_external_contact(
                str(project_a["id"]), body, fake_request
            )
        assert result["status"] == "created"

    @pytest.mark.asyncio
    async def test_delete_contact_viewer_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import delete_external_contact

        _set_role(fake_db, project_a["id"], user_b["id"], "viewer")
        fake_db.delete_external_contact = AsyncMock(
            side_effect=AssertionError("delete_external_contact called past the gate")
        )
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await delete_external_contact(
                    fake_request, str(project_a["id"]), "any-contact-id"
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_contact_editor_passes(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import delete_external_contact

        _set_role(fake_db, project_a["id"], user_b["id"], "editor")
        fake_db.delete_external_contact = AsyncMock(return_value=True)
        with _patch_caller_and_db(user_b, fake_db):
            result = await delete_external_contact(
                fake_request, str(project_a["id"]), "any-contact-id"
            )
        assert result["status"] == "deleted"

    @pytest.mark.asyncio
    async def test_add_repository_editor_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import ProjectRepositoryCreate, add_project_repository

        _set_role(fake_db, project_a["id"], user_b["id"], "editor")
        fake_db.add_project_repository = AsyncMock(
            side_effect=AssertionError("add_project_repository called past the gate")
        )
        body = ProjectRepositoryCreate(name="r", repo_url="https://example.test/r.git")
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await add_project_repository(fake_request, str(project_a["id"]), body)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_add_repository_owner_passes(
        self, user_a, project_a, fake_db, fake_request
    ):
        from main import ProjectRepositoryCreate, add_project_repository

        fake_db.add_project_repository = AsyncMock(return_value={"id": "new"})
        body = ProjectRepositoryCreate(name="r", repo_url="https://example.test/r.git")
        with _patch_caller_and_db(user_a, fake_db), patch("main.gitea_client") as gitea:
            gitea.is_initialized = False
            result = await add_project_repository(
                fake_request, str(project_a["id"]), body
            )
        assert result == {"id": "new"}

    @pytest.mark.asyncio
    async def test_update_repository_editor_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import ProjectRepositoryUpdate, update_project_repository

        _set_role(fake_db, project_a["id"], user_b["id"], "editor")
        fake_db.update_project_repository = AsyncMock(
            side_effect=AssertionError("update called past the gate")
        )
        body = ProjectRepositoryUpdate(description="x")
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await update_project_repository(
                    fake_request, str(project_a["id"]), "repo-id", body
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_remove_repository_editor_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import remove_project_repository

        _set_role(fake_db, project_a["id"], user_b["id"], "editor")
        fake_db.get_project_repository = AsyncMock(
            side_effect=AssertionError("get_project_repository called past the gate")
        )
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await remove_project_repository(
                    fake_request, str(project_a["id"]), "repo-id"
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_remove_repository_owner_passes(
        self, user_a, project_a, fake_db, fake_request
    ):
        from main import remove_project_repository

        fake_db.get_project_repository = AsyncMock(
            return_value={"id": "r", "role": "doc", "is_managed": False, "name": "r"}
        )
        fake_db.remove_project_repository = AsyncMock(
            return_value={"is_managed": False, "name": "r"}
        )
        with _patch_caller_and_db(user_a, fake_db), patch("main.gitea_client") as gitea:
            gitea.is_initialized = False
            result = await remove_project_repository(
                fake_request, str(project_a["id"]), "repo-id"
            )
        assert result == {"status": "removed"}

    @pytest.mark.asyncio
    async def test_create_project_job_viewer_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import JobCreate, create_project_job

        _set_role(fake_db, project_a["id"], user_b["id"], "viewer")
        body = JobCreate(description="x")
        with (
            _patch_caller_and_db(user_b, fake_db),
            patch(
                "main.create_job",
                AsyncMock(
                    side_effect=AssertionError("create_job called past the gate")
                ),
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await create_project_job(fake_request, str(project_a["id"]), body)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_create_project_job_editor_passes(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import JobCreate, create_project_job

        _set_role(fake_db, project_a["id"], user_b["id"], "editor")
        body = JobCreate(description="x")
        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.create_job", AsyncMock(return_value={"id": "new-job"})),
        ):
            result = await create_project_job(fake_request, str(project_a["id"]), body)
        assert result == {"id": "new-job"}
        assert body.project_id == str(project_a["id"])
