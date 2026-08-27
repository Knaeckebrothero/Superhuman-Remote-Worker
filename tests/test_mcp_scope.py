"""F7 — MCP scope plumbing across the access.py visibility helpers.

The token's legacy MCP scope string lives in ``user['scopes'][0]`` once
auth.py's ``_get_user_from_mcp_headers`` has run. Values:

  - ``''`` / missing            → no narrowing
  - ``'user'`` / ``'all'``      → no narrowing at this layer (admin power
                                   comes from realm role, not from scope)
  - ``'project:<uuid>'``        → restrict to that one project

These tests construct user dicts with each scope shape and verify the
access.py helpers narrow accordingly. Helpers covered:

  - _scope_project_id / _scope_permits_project / _scope_permits_personal
  - user_visible_project_ids
  - require_project_member / require_project_owner
  - require_job_access / user_can_access_job
  - user_can_access_ide_entity
  - require_sudo_request_authority
  - user_can_access_datasource / require_datasource_owner
  - apply_mcp_scope (SQL-shaped variant, already had coverage; refresh here)
"""

from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from security import access


def _scoped(user: dict, scope: str | None) -> dict:
    """Return a copy of ``user`` carrying the given MCP scope."""
    out = dict(user)
    out["auth_method"] = "mcp"
    out["scopes"] = [scope] if scope else []
    return out


def _patch_caller(user: dict):
    return patch.object(access, "require_approved_user", AsyncMock(return_value=user))


# =============================================================================
# Scope-guard helpers (pure functions)
# =============================================================================


class TestScopeProjectId:
    def test_no_scope_returns_none(self, user_a):
        assert access._scope_project_id(user_a) is None

    def test_user_scope_returns_none(self, user_a):
        assert access._scope_project_id(_scoped(user_a, "user")) is None

    def test_all_scope_returns_none(self, user_a):
        assert access._scope_project_id(_scoped(user_a, "all")) is None

    def test_project_scope_returns_uuid(self, user_a, project_a):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        assert access._scope_project_id(scoped) == project_a["id"]

    def test_malformed_project_scope_returns_sentinel(self, user_a):
        # Per docstring: an unparseable project: scope becomes a permanent
        # all-zero UUID so callers fail closed.
        scoped = _scoped(user_a, "project:not-a-uuid")
        result = access._scope_project_id(scoped)
        assert result == UUID("00000000-0000-0000-0000-000000000000")


class TestScopePermitsProject:
    def test_no_scope_allows_anything(self, user_a, project_a):
        assert access._scope_permits_project(user_a, str(project_a["id"]))
        assert access._scope_permits_project(user_a, None)

    def test_matching_project_scope_allows(self, user_a, project_a):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        assert access._scope_permits_project(scoped, str(project_a["id"]))

    def test_mismatched_project_scope_denies(self, user_a, project_a, project_b):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        assert not access._scope_permits_project(scoped, str(project_b["id"]))

    def test_project_scope_denies_none_project(self, user_a, project_a):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        assert not access._scope_permits_project(scoped, None)


class TestScopePermitsPersonal:
    def test_no_scope_allows(self, user_a):
        assert access._scope_permits_personal(user_a)

    def test_user_scope_allows(self, user_a):
        assert access._scope_permits_personal(_scoped(user_a, "user"))

    def test_all_scope_allows(self, user_a):
        assert access._scope_permits_personal(_scoped(user_a, "all"))

    def test_project_scope_denies(self, user_a, project_a):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        assert not access._scope_permits_personal(scoped)


# =============================================================================
# user_visible_project_ids — narrowing for admin and non-admin
# =============================================================================


class TestUserVisibleProjectIdsScoped:
    @pytest.mark.asyncio
    async def test_admin_with_project_scope_narrows(
        self, user_admin, project_a, fake_db
    ):
        scoped = _scoped(user_admin, f"project:{project_a['id']}")
        result = await access.user_visible_project_ids(scoped, fake_db)
        assert result == {project_a["id"]}

    @pytest.mark.asyncio
    async def test_user_with_matching_scope_passes(self, user_a, project_a, fake_db):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        result = await access.user_visible_project_ids(scoped, fake_db)
        assert result == {project_a["id"]}

    @pytest.mark.asyncio
    async def test_user_with_other_project_scope_returns_empty(
        self, user_a, project_b, fake_db
    ):
        scoped = _scoped(user_a, f"project:{project_b['id']}")
        result = await access.user_visible_project_ids(scoped, fake_db)
        assert result == set()

    @pytest.mark.asyncio
    async def test_user_scope_is_noop(self, user_a, project_a, fake_db):
        scoped = _scoped(user_a, "user")
        result = await access.user_visible_project_ids(scoped, fake_db)
        assert result == {project_a["id"]}

    @pytest.mark.asyncio
    async def test_all_scope_is_noop(self, user_admin, fake_db):
        scoped = _scoped(user_admin, "all")
        result = await access.user_visible_project_ids(scoped, fake_db)
        assert result == "all"


# =============================================================================
# require_project_member / require_project_owner with scope
# =============================================================================


class TestRequireProjectMemberScoped:
    @pytest.mark.asyncio
    async def test_matching_scope_passes(
        self, user_a, project_a, fake_db, fake_request
    ):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        with _patch_caller(scoped):
            user, project = await access.require_project_member(
                fake_request, fake_db, str(project_a["id"])
            )
        assert user is scoped

    @pytest.mark.asyncio
    async def test_mismatched_scope_403(
        self, user_a, project_a, project_b, fake_db, fake_request
    ):
        # user_a is a member of project_a, but their token is scoped to project_b
        scoped = _scoped(user_a, f"project:{project_b['id']}")
        with _patch_caller(scoped):
            with pytest.raises(HTTPException) as exc:
                await access.require_project_member(
                    fake_request, fake_db, str(project_a["id"])
                )
        assert exc.value.status_code == 403
        assert "scope" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_admin_with_mismatched_scope_403(
        self, user_admin, project_a, project_b, fake_db, fake_request
    ):
        """Admin power is restricted by the token's scope."""
        scoped = _scoped(user_admin, f"project:{project_b['id']}")
        with _patch_caller(scoped):
            with pytest.raises(HTTPException) as exc:
                await access.require_project_member(
                    fake_request, fake_db, str(project_a["id"])
                )
        assert exc.value.status_code == 403


class TestRequireProjectOwnerScoped:
    @pytest.mark.asyncio
    async def test_mismatched_scope_403(
        self, user_a, project_a, project_b, fake_db, fake_request
    ):
        scoped = _scoped(user_a, f"project:{project_b['id']}")
        with _patch_caller(scoped):
            with pytest.raises(HTTPException) as exc:
                await access.require_project_owner(
                    fake_request, fake_db, str(project_a["id"])
                )
        assert exc.value.status_code == 403


# =============================================================================
# require_job_access / user_can_access_job with scope
# =============================================================================


class TestRequireJobAccessScoped:
    @pytest.mark.asyncio
    async def test_matching_scope_passes(
        self, user_a, job_a, project_a, fake_db, fake_request
    ):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        with _patch_caller(scoped):
            user, job = await access.require_job_access(
                fake_request, fake_db, str(job_a["id"])
            )
        assert user is scoped

    @pytest.mark.asyncio
    async def test_mismatched_scope_403(
        self, user_a, job_a, project_b, fake_db, fake_request
    ):
        scoped = _scoped(user_a, f"project:{project_b['id']}")
        with _patch_caller(scoped):
            with pytest.raises(HTTPException) as exc:
                await access.require_job_access(fake_request, fake_db, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_restricted_by_scope(
        self, user_admin, job_a, project_b, fake_db, fake_request
    ):
        scoped = _scoped(user_admin, f"project:{project_b['id']}")
        with _patch_caller(scoped):
            with pytest.raises(HTTPException) as exc:
                await access.require_job_access(fake_request, fake_db, str(job_a["id"]))
        assert exc.value.status_code == 403


class TestUserCanAccessJobScoped:
    @pytest.mark.asyncio
    async def test_matching_scope_true(self, user_a, job_a, project_a, fake_db):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        assert await access.user_can_access_job(scoped, fake_db, str(job_a["id"]))

    @pytest.mark.asyncio
    async def test_mismatched_scope_false(self, user_a, job_a, project_b, fake_db):
        scoped = _scoped(user_a, f"project:{project_b['id']}")
        assert not await access.user_can_access_job(scoped, fake_db, str(job_a["id"]))

    @pytest.mark.asyncio
    async def test_admin_with_scope_restricted(
        self, user_admin, job_a, project_b, fake_db
    ):
        scoped = _scoped(user_admin, f"project:{project_b['id']}")
        assert not await access.user_can_access_job(scoped, fake_db, str(job_a["id"]))

    @pytest.mark.asyncio
    async def test_none_job_id_admin_with_project_scope_denied(
        self, user_admin, project_a, fake_db
    ):
        """Project-scoped admin can't see orphan events."""
        scoped = _scoped(user_admin, f"project:{project_a['id']}")
        assert not await access.user_can_access_job(scoped, fake_db, None)


# =============================================================================
# user_can_access_ide_entity — jobs (scoped) vs threads (personal-only)
# =============================================================================


class TestUserCanAccessIdeEntityScoped:
    @pytest.mark.asyncio
    async def test_job_matching_scope_true(self, user_a, job_a, project_a, fake_db):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        assert await access.user_can_access_ide_entity(
            scoped, fake_db, str(job_a["id"])
        )

    @pytest.mark.asyncio
    async def test_job_mismatched_scope_false(self, user_a, job_a, project_b, fake_db):
        scoped = _scoped(user_a, f"project:{project_b['id']}")
        assert not await access.user_can_access_ide_entity(
            scoped, fake_db, str(job_a["id"])
        )

    @pytest.mark.asyncio
    async def test_thread_with_project_scope_denied(
        self, user_a, thread_a, project_a, fake_db
    ):
        """Threads are personal — project-scoped tokens can't open them."""
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        assert not await access.user_can_access_ide_entity(
            scoped, fake_db, str(thread_a["id"])
        )

    @pytest.mark.asyncio
    async def test_thread_with_user_scope_allowed(self, user_a, thread_a, fake_db):
        scoped = _scoped(user_a, "user")
        assert await access.user_can_access_ide_entity(
            scoped, fake_db, str(thread_a["id"])
        )


# =============================================================================
# require_sudo_request_authority — scope check via underlying job's project
# =============================================================================


class TestRequireSudoRequestAuthorityScoped:
    @pytest.mark.asyncio
    async def test_admin_with_matching_scope_passes(
        self, user_admin, job_a, project_a, fake_db, fake_request
    ):
        scoped = _scoped(user_admin, f"project:{project_a['id']}")
        sudo_req = {"id": "req-1", "job_id": str(job_a["id"])}
        with patch("services.sudo_gate.sudo_gate") as gate:
            gate.get_request = AsyncMock(return_value=sudo_req)
            with _patch_caller(scoped):
                result = await access.require_sudo_request_authority(
                    fake_request, fake_db, "req-1"
                )
        assert result is sudo_req

    @pytest.mark.asyncio
    async def test_admin_with_mismatched_scope_403(
        self, user_admin, job_a, project_b, fake_db, fake_request
    ):
        scoped = _scoped(user_admin, f"project:{project_b['id']}")
        sudo_req = {"id": "req-1", "job_id": str(job_a["id"])}
        with patch("services.sudo_gate.sudo_gate") as gate:
            gate.get_request = AsyncMock(return_value=sudo_req)
            with _patch_caller(scoped):
                with pytest.raises(HTTPException) as exc:
                    await access.require_sudo_request_authority(
                        fake_request, fake_db, "req-1"
                    )
        assert exc.value.status_code == 403


# =============================================================================
# Datasource access with scope
# =============================================================================


class TestUserCanAccessDatasourceScoped:
    @pytest.mark.asyncio
    async def test_linked_project_scope_allows(
        self, user_a, datasource_a, project_a, fake_db
    ):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        assert await access.user_can_access_datasource(scoped, fake_db, datasource_a)

    @pytest.mark.asyncio
    async def test_unlinked_project_scope_denies_creator(
        self, user_a, datasource_a, project_b, fake_db
    ):
        """Creator-only access doesn't survive a project-scope mismatch."""
        scoped = _scoped(user_a, f"project:{project_b['id']}")
        assert not await access.user_can_access_datasource(
            scoped, fake_db, datasource_a
        )

    @pytest.mark.asyncio
    async def test_unlinked_project_scope_denies_admin(
        self, user_admin, datasource_a, project_b, fake_db
    ):
        scoped = _scoped(user_admin, f"project:{project_b['id']}")
        assert not await access.user_can_access_datasource(
            scoped, fake_db, datasource_a
        )


class TestRequireDatasourceOwnerScoped:
    @pytest.mark.asyncio
    async def test_creator_with_mismatched_scope_403(
        self, user_a, datasource_a, project_b, fake_db, fake_request
    ):
        scoped = _scoped(user_a, f"project:{project_b['id']}")
        with _patch_caller(scoped):
            with pytest.raises(HTTPException) as exc:
                await access.require_datasource_owner(
                    fake_request, fake_db, str(datasource_a["id"])
                )
        assert exc.value.status_code == 403


# =============================================================================
# apply_mcp_scope — existing SQL fragment behavior with new shape
# =============================================================================


class TestApplyMcpScopeWithNewShape:
    def test_user_scope_noop(self, user_a):
        scoped = _scoped(user_a, "user")
        clause, params = access.apply_mcp_scope(scoped)
        assert clause == ""
        assert params == {}

    def test_project_scope_narrows(self, user_a, project_a):
        scoped = _scoped(user_a, f"project:{project_a['id']}")
        clause, params = access.apply_mcp_scope(scoped)
        assert "project_id" in clause
        assert params["scope_project"] == project_a["id"]


# =============================================================================
# Auth-side: _get_user_from_mcp_headers stashes scope in user dict
# =============================================================================


class TestMcpHeadersStashScope:
    @pytest.mark.asyncio
    async def test_scope_header_landed_in_user(self, user_a, monkeypatch):
        """The auth path must read X-MCP-Scope and write it to user['scopes']."""
        from unittest.mock import MagicMock

        from security.auth import _get_user_from_mcp_headers

        monkeypatch.setenv("MCP_INTERNAL_KEY", "shared-secret")
        request = MagicMock()
        request.headers = {
            "X-MCP-User-Id": str(user_a["id"]),
            "X-Internal-Key": "shared-secret",
            "X-MCP-Scope": "project:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        }
        db = AsyncMock()
        db.get_user = AsyncMock(return_value=dict(user_a))

        resolved = await _get_user_from_mcp_headers(request, db)
        assert resolved["auth_method"] == "mcp"
        assert resolved["scopes"] == ["project:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]

    @pytest.mark.asyncio
    async def test_missing_scope_header_yields_empty_list(self, user_a, monkeypatch):
        from unittest.mock import MagicMock

        from security.auth import _get_user_from_mcp_headers

        monkeypatch.setenv("MCP_INTERNAL_KEY", "shared-secret")
        request = MagicMock()
        request.headers = {
            "X-MCP-User-Id": str(user_a["id"]),
            "X-Internal-Key": "shared-secret",
        }
        db = AsyncMock()
        db.get_user = AsyncMock(return_value=dict(user_a))

        resolved = await _get_user_from_mcp_headers(request, db)
        assert resolved["scopes"] == []
