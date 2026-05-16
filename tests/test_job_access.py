"""G1 — multi-tenancy gates on the jobs read family.

Covers the 30 read endpoints under ``/api/jobs`` and ``/api/jobs/{id}/...``
that G1 brings under the visibility model:

* ``list_jobs`` (admin-only cross-user filter; visibility OR for non-admins;
  MCP project scope narrowing)
* ``get_job`` (owner / project-member / admin)
* The 28 get-by-id read endpoints (audit, llm-requests, citations, memories,
  workspace files, todos, repo, bulk caches, etc.) — every one calls
  ``require_job_access`` before doing any work.

Tests share the 3-user / 2-project fixture from ``conftest.py``. The
``_patch_caller_and_db`` helper mirrors the F2-F6 pattern of replacing
both ``main.require_approved_user`` AND ``security.access.require_approved_user``
(handlers call the main copy; ``require_job_access`` itself calls the
access-module copy).
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

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


def _patch_mongo_unavailable():
    """Make ``main.mongodb.is_available`` False so list_jobs skips enrichment."""
    fake_mongo = MagicMock()
    fake_mongo.is_available = False
    return patch("main.mongodb", fake_mongo)


def _scoped(user: dict, scope: str) -> dict:
    """Return a copy of ``user`` carrying the given MCP scope string."""
    out = dict(user)
    out["scopes"] = [scope]
    out["auth_method"] = "mcp"
    return out


# =============================================================================
# list_jobs — visibility model + admin-only cross-user filter
# =============================================================================


class TestListJobs:
    @pytest.mark.asyncio
    async def test_non_admin_uses_visible_jobs_helper(
        self, user_a, fake_db, fake_request
    ):
        """Caller's own + project-member jobs via OR-clause."""
        from main import list_jobs

        fake_db.get_visible_jobs = AsyncMock(return_value=[])
        fake_db.get_jobs = AsyncMock(return_value=[])
        with _patch_caller_and_db(user_a, fake_db), _patch_mongo_unavailable():
            await list_jobs(fake_request, status=None, user_id=None, limit=100)

        fake_db.get_visible_jobs.assert_awaited_once()
        fake_db.get_jobs.assert_not_awaited()
        kwargs = fake_db.get_visible_jobs.call_args.kwargs
        assert kwargs["owner_user_id"] == str(user_a["id"])
        # user_a is the owner of project_a → that one project_id appears.
        assert len(kwargs["visible_project_ids"]) == 1
        assert kwargs["scope_project_id"] is None

    @pytest.mark.asyncio
    async def test_non_admin_with_empty_membership_still_sees_own_jobs(
        self, user_a, fake_db, fake_request
    ):
        """Empty visible_project_ids → OR-clause still returns own-user jobs."""
        from main import list_jobs

        # Strip user_a's project memberships.
        fake_db.get_projects_for_user = AsyncMock(return_value=[])
        fake_db.get_visible_jobs = AsyncMock(return_value=[])
        with _patch_caller_and_db(user_a, fake_db), _patch_mongo_unavailable():
            await list_jobs(fake_request, status=None, user_id=None, limit=100)

        kwargs = fake_db.get_visible_jobs.call_args.kwargs
        assert kwargs["owner_user_id"] == str(user_a["id"])
        assert kwargs["visible_project_ids"] == []

    @pytest.mark.asyncio
    async def test_admin_uses_unfiltered_get_jobs(
        self, user_admin, fake_db, fake_request
    ):
        from main import list_jobs

        fake_db.get_jobs = AsyncMock(return_value=[])
        fake_db.get_visible_jobs = AsyncMock(return_value=[])
        with _patch_caller_and_db(user_admin, fake_db), _patch_mongo_unavailable():
            await list_jobs(fake_request, status=None, user_id=None, limit=100)

        fake_db.get_jobs.assert_awaited_once()
        fake_db.get_visible_jobs.assert_not_awaited()
        kwargs = fake_db.get_jobs.call_args.kwargs
        assert kwargs["user_id"] is None
        assert kwargs["scope_project_id"] is None

    @pytest.mark.asyncio
    async def test_admin_cross_user_query_passes_user_id_through(
        self, user_admin, user_a, fake_db, fake_request
    ):
        from main import list_jobs

        fake_db.get_jobs = AsyncMock(return_value=[])
        with _patch_caller_and_db(user_admin, fake_db), _patch_mongo_unavailable():
            await list_jobs(
                fake_request, status=None, user_id=str(user_a["id"]), limit=100
            )

        kwargs = fake_db.get_jobs.call_args.kwargs
        assert kwargs["user_id"] == str(user_a["id"])

    @pytest.mark.asyncio
    async def test_non_admin_cross_user_query_403(
        self, user_a, user_b, fake_db, fake_request
    ):
        from main import list_jobs

        with _patch_caller_and_db(user_a, fake_db), _patch_mongo_unavailable():
            with pytest.raises(HTTPException) as exc:
                await list_jobs(
                    fake_request, status=None, user_id=str(user_b["id"]), limit=100
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_non_admin_self_query_allowed(self, user_a, fake_db, fake_request):
        """Passing ?user_id=<self> is redundant but not rejected."""
        from main import list_jobs

        fake_db.get_visible_jobs = AsyncMock(return_value=[])
        with _patch_caller_and_db(user_a, fake_db), _patch_mongo_unavailable():
            await list_jobs(
                fake_request, status=None, user_id=str(user_a["id"]), limit=100
            )

        fake_db.get_visible_jobs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_status_filter_passes_through(self, user_a, fake_db, fake_request):
        from main import list_jobs

        fake_db.get_visible_jobs = AsyncMock(return_value=[])
        with _patch_caller_and_db(user_a, fake_db), _patch_mongo_unavailable():
            await list_jobs(fake_request, status="completed", user_id=None, limit=50)

        kwargs = fake_db.get_visible_jobs.call_args.kwargs
        assert kwargs["status"] == "completed"
        assert kwargs["limit"] == 50

    @pytest.mark.asyncio
    async def test_mcp_project_scope_narrows_admin(
        self, user_admin, project_a, fake_db, fake_request
    ):
        """An MCP `project:<uuid>` scope adds AND project_id = <pid> for admins."""
        from main import list_jobs

        scoped = _scoped(user_admin, f"project:{project_a['id']}")
        fake_db.get_jobs = AsyncMock(return_value=[])
        with _patch_caller_and_db(scoped, fake_db), _patch_mongo_unavailable():
            await list_jobs(fake_request, status=None, user_id=None, limit=100)

        kwargs = fake_db.get_jobs.call_args.kwargs
        assert kwargs["scope_project_id"] == str(project_a["id"])

    @pytest.mark.asyncio
    async def test_mcp_project_scope_narrows_non_admin(
        self, user_a, project_a, fake_db, fake_request
    ):
        from main import list_jobs

        scoped = _scoped(user_a, f"project:{project_a['id']}")
        fake_db.get_visible_jobs = AsyncMock(return_value=[])
        with _patch_caller_and_db(scoped, fake_db), _patch_mongo_unavailable():
            await list_jobs(fake_request, status=None, user_id=None, limit=100)

        kwargs = fake_db.get_visible_jobs.call_args.kwargs
        assert kwargs["scope_project_id"] == str(project_a["id"])

    @pytest.mark.asyncio
    async def test_unauthenticated_baseline(self, fake_db, fake_request):
        """If `require_approved_user` raises, no DB call happens."""
        from main import list_jobs

        fake_db.get_visible_jobs = AsyncMock(return_value=[])
        fake_db.get_jobs = AsyncMock(return_value=[])
        with (
            patch(
                "main.require_approved_user",
                AsyncMock(side_effect=HTTPException(status_code=401)),
            ),
            patch("main.postgres_db", fake_db),
            _patch_mongo_unavailable(),
        ):
            with pytest.raises(HTTPException) as exc:
                await list_jobs(fake_request, status=None, user_id=None, limit=100)
        assert exc.value.status_code == 401
        fake_db.get_visible_jobs.assert_not_awaited()
        fake_db.get_jobs.assert_not_awaited()


# =============================================================================
# get_job — owner / project-member / admin / scope
# =============================================================================


class TestGetJob:
    @pytest.mark.asyncio
    async def test_owner_passes(self, user_a, job_a, fake_db, fake_request):
        from main import get_job

        with _patch_caller_and_db(user_a, fake_db), _patch_mongo_unavailable():
            result = await get_job(fake_request, str(job_a["id"]))
        # The handler enriches with audit_count=None when mongo unavailable
        assert result["id"] == job_a["id"]
        assert "audit_count" in result

    @pytest.mark.asyncio
    async def test_cross_user_403(self, user_b, job_a, fake_db, fake_request):
        from main import get_job

        with _patch_caller_and_db(user_b, fake_db), _patch_mongo_unavailable():
            with pytest.raises(HTTPException) as exc:
                await get_job(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_404(self, user_a, fake_db, fake_request):
        from main import get_job

        with _patch_caller_and_db(user_a, fake_db), _patch_mongo_unavailable():
            with pytest.raises(HTTPException) as exc:
                await get_job(fake_request, "00000000-0000-0000-0000-000000000999")
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_bypass(self, user_admin, job_a, fake_db, fake_request):
        from main import get_job

        with _patch_caller_and_db(user_admin, fake_db), _patch_mongo_unavailable():
            result = await get_job(fake_request, str(job_a["id"]))
        assert result["id"] == job_a["id"]

    @pytest.mark.asyncio
    async def test_project_member_passes(
        self, user_a, user_b, job_a, fake_db, fake_request
    ):
        """user_b added as viewer on project_a → can see job_a."""
        from main import get_job
        from uuid import UUID

        async def member_lookup(pid, uid):
            # user_a is owner of project_a (default); user_b is also a viewer.
            if UUID(str(uid)) == user_b["id"]:
                return "viewer"
            if UUID(str(uid)) == user_a["id"]:
                return "owner"
            return None

        fake_db.get_user_role_in_project = AsyncMock(side_effect=member_lookup)
        with _patch_caller_and_db(user_b, fake_db), _patch_mongo_unavailable():
            result = await get_job(fake_request, str(job_a["id"]))
        assert result["id"] == job_a["id"]

    @pytest.mark.asyncio
    async def test_mcp_project_scope_mismatch_403(
        self, user_admin, job_a, project_b, fake_db, fake_request
    ):
        """Even an admin with a `project:<other>` scope is denied for an outside job."""
        from main import get_job

        scoped = _scoped(user_admin, f"project:{project_b['id']}")
        with _patch_caller_and_db(scoped, fake_db), _patch_mongo_unavailable():
            with pytest.raises(HTTPException) as exc:
                await get_job(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403


# =============================================================================
# Gated read endpoints — gate fires before service is hit
# =============================================================================
#
# Picks one endpoint per backend so we prove the gate is wired everywhere:
#   MongoDB-backed   → get_job_audit, get_job_llm_requests
#   workspace svc    → get_job_workspace, get_workspace_file
#   gitea (repo)     → list_repo_contents, get_repo_file
#   vector_db (PG)   → list_job_citations, list_job_memories,
#                       get_citation_stats, get_memory_stats,
#                       search_job_sources
#   snapshot svc     → get_job_snapshot
#   agent proxy      → get_job_shell_state (gates before status check)
#   misc             → get_frozen_job_data, get_job_progress, get_job_version
#                       list_message_threads
#
# Each test calls the endpoint with a cross-user caller; the gate must
# raise 403 before any downstream service mock is touched.


def _make_dud(name: str):
    """Return a magic that explodes on attribute access.

    If the gate ever lets the call through, the downstream code blows up
    in a recognisable way — proving the gate is the only thing stopping
    the request.
    """
    return MagicMock(side_effect=AssertionError(f"{name} called past the gate"))


class TestGatedReadEndpoints:
    @pytest.mark.asyncio
    async def test_get_job_audit_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_job_audit

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.mongodb", _make_dud("mongodb")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_job_audit(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_job_llm_requests_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_job_llm_requests

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.mongodb", _make_dud("mongodb")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_job_llm_requests(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_job_workspace_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_job_workspace

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.workspace_service", _make_dud("workspace_service")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_job_workspace(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_workspace_file_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_workspace_file

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.workspace_service", _make_dud("workspace_service")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_workspace_file(fake_request, str(job_a["id"]), "workspace.md")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_repo_contents_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import list_repo_contents

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.gitea_client", _make_dud("gitea_client")),
        ):
            with pytest.raises(HTTPException) as exc:
                await list_repo_contents(
                    fake_request, str(job_a["id"]), path="", ref=None
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_repo_file_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_repo_file

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.gitea_client", _make_dud("gitea_client")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_repo_file(
                    fake_request, str(job_a["id"]), path="README.md", ref=None
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_job_citations_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import list_job_citations

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.vector_db", _make_dud("vector_db")),
        ):
            with pytest.raises(HTTPException) as exc:
                await list_job_citations(
                    fake_request,
                    str(job_a["id"]),
                    source_id=None,
                    status=None,
                    limit=50,
                    offset=0,
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_job_memories_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import list_job_memories

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.vector_db", _make_dud("vector_db")),
        ):
            with pytest.raises(HTTPException) as exc:
                await list_job_memories(
                    fake_request,
                    str(job_a["id"]),
                    memory_type=None,
                    source=None,
                    search=None,
                    sort_by="created_at",
                    sort_order="desc",
                    limit=50,
                    offset=0,
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_memory_stats_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_memory_stats

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.vector_db", _make_dud("vector_db")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_memory_stats(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_citation_stats_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_citation_stats

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.vector_db", _make_dud("vector_db")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_citation_stats(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_search_job_sources_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import search_job_sources

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.vector_db", _make_dud("vector_db")),
        ):
            with pytest.raises(HTTPException) as exc:
                await search_job_sources(
                    fake_request,
                    str(job_a["id"]),
                    query="q",
                    mode="keyword",
                    source_type=None,
                    tags=None,
                    top_k=10,
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_job_snapshot_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_job_snapshot

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.snapshot_service", _make_dud("snapshot_service")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_job_snapshot(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_job_shell_state_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_job_shell_state

        # Mark the job as not-processing — if the gate let through, we'd
        # see 400 from the status check instead of 403.
        fake_db.get_job = AsyncMock(return_value={**job_a, "status": "completed"})
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_job_shell_state(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_frozen_job_data_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_frozen_job_data

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_frozen_job_data(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_job_progress_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_job_progress

        fake_db.get_job_progress = AsyncMock(
            side_effect=AssertionError("progress called past the gate")
        )
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_job_progress(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_job_version_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import get_job_version

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.mongodb", _make_dud("mongodb")),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_job_version(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_message_threads_blocked_cross_user(
        self, user_b, job_a, fake_db, fake_request
    ):
        from main import list_message_threads

        fake_db.get_message_threads = AsyncMock(
            side_effect=AssertionError("threads called past the gate")
        )
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await list_message_threads(fake_request, str(job_a["id"]))
        assert exc.value.status_code == 403


# =============================================================================
# Positive paths — owner gets through, gate doesn't over-block
# =============================================================================


class TestGatedReadEndpointsHappyPath:
    @pytest.mark.asyncio
    async def test_get_job_audit_owner_reaches_mongo(
        self, user_a, job_a, fake_db, fake_request
    ):
        from main import get_job_audit

        fake_mongo = MagicMock()
        fake_mongo.is_available = True
        fake_mongo.get_job_audit = AsyncMock(return_value={"entries": [], "total": 0})
        with _patch_caller_and_db(user_a, fake_db), patch("main.mongodb", fake_mongo):
            await get_job_audit(fake_request, str(job_a["id"]))
        fake_mongo.get_job_audit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_get_job_workspace_owner_reaches_service(
        self, user_a, job_a, fake_db, fake_request
    ):
        from main import get_job_workspace

        fake_svc = MagicMock()
        fake_svc.get_workspace_overview = MagicMock(return_value={"files": []})
        with (
            _patch_caller_and_db(user_a, fake_db),
            patch("main.workspace_service", fake_svc),
        ):
            result = await get_job_workspace(fake_request, str(job_a["id"]))
        assert result == {"files": []}

    @pytest.mark.asyncio
    async def test_get_job_progress_admin_reaches_db(
        self, user_admin, job_a, fake_db, fake_request
    ):
        from main import get_job_progress

        fake_db.get_job_progress = AsyncMock(return_value={"status": "ok"})
        with _patch_caller_and_db(user_admin, fake_db):
            result = await get_job_progress(fake_request, str(job_a["id"]))
        assert result == {"status": "ok"}
