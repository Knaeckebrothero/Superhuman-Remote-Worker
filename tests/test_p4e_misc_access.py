"""P4e — expert + datasource + misc reads.

Five endpoints that didn't fit any earlier bundle's natural home:

* `GET /api/experts`                            → approved-user only
* `GET /api/experts/{expert_id}`                → approved-user only
* `POST /api/datasources/ssh-keys/generate`     → approved-user only
* `GET /api/actions/pending`                    → approved + per-user filter
* `GET /api/citations/{citation_id}`            → caller-can-access-linked-job

Tests share the 3-user / 2-project fixture from ``conftest.py``. They
verify the post-gate logic (the gate itself is tested by F1) — admin vs
non-admin behavior for pending-actions, cross-user 404 for citations,
and that the gated endpoints actually run the gate (otherwise the
downstream service mock would explode).
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


def _patch_caller_and_db(user: dict, db):
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


class TestExpertsGated:
    @pytest.mark.asyncio
    async def test_list_experts_runs_gate(self, user_a, fake_db, fake_request):
        """The gate fires before the global cache is touched. We patch
        ``require_approved_user`` to make it raise, then assert the call
        bubbles 403."""
        from main import list_experts

        async def _denied(*_a, **_kw):
            raise HTTPException(status_code=403, detail="denied")

        with patch("main.require_approved_user", AsyncMock(side_effect=_denied)):
            with pytest.raises(HTTPException) as exc:
                await list_experts(fake_request)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_expert_runs_gate(self, user_a, fake_db, fake_request):
        from main import get_expert

        async def _denied(*_a, **_kw):
            raise HTTPException(status_code=403, detail="denied")

        with patch("main.require_approved_user", AsyncMock(side_effect=_denied)):
            with pytest.raises(HTTPException) as exc:
                await get_expert(fake_request, "scholar")
        assert exc.value.status_code == 403


class TestSshKeyGenerateGated:
    @pytest.mark.asyncio
    async def test_runs_gate(self, fake_request):
        from main import generate_datasource_ssh_key

        async def _denied(*_a, **_kw):
            raise HTTPException(status_code=403, detail="denied")

        with patch("main.require_approved_user", AsyncMock(side_effect=_denied)):
            with pytest.raises(HTTPException) as exc:
                await generate_datasource_ssh_key(fake_request)
        assert exc.value.status_code == 403


class TestPendingActions:
    @pytest.mark.asyncio
    async def test_admin_uses_unfiltered_db_call(
        self, user_admin, fake_db, fake_request
    ):
        """Admin path passes no owner filter — DB returns global counts."""
        from main import _pending_actions_cache, get_pending_actions

        _pending_actions_cache.clear()
        fake_db.get_pending_action_counts = AsyncMock(
            return_value={"counts": {"total": 99}, "most_urgent": None}
        )
        with _patch_caller_and_db(user_admin, fake_db):
            await get_pending_actions(fake_request)

        fake_db.get_pending_action_counts.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_non_admin_filters_by_user_and_projects(
        self, user_a, fake_db, fake_request
    ):
        """Non-admin path resolves caller's project memberships and passes
        owner_user_id + visible_project_ids — narrowing the DB query."""
        from main import _pending_actions_cache, get_pending_actions

        _pending_actions_cache.clear()
        fake_db.get_pending_action_counts = AsyncMock(
            return_value={"counts": {"total": 3}, "most_urgent": None}
        )
        with _patch_caller_and_db(user_a, fake_db):
            await get_pending_actions(fake_request)

        kwargs = fake_db.get_pending_action_counts.call_args.kwargs
        assert kwargs["owner_user_id"] == str(user_a["id"])
        # user_a owns project_a → exactly one membership.
        assert len(kwargs["visible_project_ids"]) == 1

    @pytest.mark.asyncio
    async def test_cache_segregates_admin_and_non_admin(
        self, user_a, user_admin, fake_db, fake_request
    ):
        """Two callers with different visibility hit the DB twice (different
        cache keys). Without the fix, the admin's first response would have
        leaked into user_a's slot."""
        from main import _pending_actions_cache, get_pending_actions

        _pending_actions_cache.clear()
        fake_db.get_pending_action_counts = AsyncMock(
            return_value={"counts": {"total": 0}, "most_urgent": None}
        )
        with _patch_caller_and_db(user_admin, fake_db):
            await get_pending_actions(fake_request)
        with _patch_caller_and_db(user_a, fake_db):
            await get_pending_actions(fake_request)

        assert fake_db.get_pending_action_counts.await_count == 2

    @pytest.mark.asyncio
    async def test_runs_gate(self, fake_request):
        from main import _pending_actions_cache, get_pending_actions

        _pending_actions_cache.clear()

        async def _denied(*_a, **_kw):
            raise HTTPException(status_code=403, detail="denied")

        with patch("main.require_approved_user", AsyncMock(side_effect=_denied)):
            with pytest.raises(HTTPException) as exc:
                await get_pending_actions(fake_request)
        assert exc.value.status_code == 403


class TestCitationDetail:
    @pytest.mark.asyncio
    async def test_runs_gate(self, fake_request):
        from main import get_citation_detail

        async def _denied(*_a, **_kw):
            raise HTTPException(status_code=403, detail="denied")

        with patch("main.require_approved_user", AsyncMock(side_effect=_denied)):
            with pytest.raises(HTTPException) as exc:
                await get_citation_detail(fake_request, 1)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_citation_404(self, user_a, fake_db, fake_request):
        from main import get_citation_detail

        fake_conn = MagicMock()
        fake_conn.fetchrow = AsyncMock(return_value=None)
        fake_vector_db = MagicMock()
        fake_vector_db.acquire.return_value.__aenter__ = AsyncMock(
            return_value=fake_conn
        )
        fake_vector_db.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            _patch_caller_and_db(user_a, fake_db),
            patch("main.vector_db", fake_vector_db),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_citation_detail(fake_request, 9999)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_cross_user_returns_404_not_403(
        self, user_b, job_a, fake_db, fake_request
    ):
        """Citation links to job_a (owned by user_a). user_b is a stranger
        to project_a, so user_can_access_any_job returns False and the
        endpoint 404s (probe-resistant — doesn't leak existence)."""
        from main import get_citation_detail

        fake_conn = MagicMock()
        fake_conn.fetchrow = AsyncMock(
            return_value={
                "id": 1,
                "job_id": str(job_a["id"]),
                "claim": "x",
                "verbatim_quote": "y",
            }
        )
        fake_vector_db = MagicMock()
        fake_vector_db.acquire.return_value.__aenter__ = AsyncMock(
            return_value=fake_conn
        )
        fake_vector_db.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main.vector_db", fake_vector_db),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_citation_detail(fake_request, 1)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_owner_succeeds(self, user_a, job_a, fake_db, fake_request):
        from main import get_citation_detail

        row = {
            "id": 1,
            "job_id": str(job_a["id"]),
            "claim": "x",
            "verbatim_quote": "y",
        }
        fake_conn = MagicMock()
        fake_conn.fetchrow = AsyncMock(return_value=row)
        fake_vector_db = MagicMock()
        fake_vector_db.acquire.return_value.__aenter__ = AsyncMock(
            return_value=fake_conn
        )
        fake_vector_db.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            _patch_caller_and_db(user_a, fake_db),
            patch("main.vector_db", fake_vector_db),
        ):
            result = await get_citation_detail(fake_request, 1)
        assert result["id"] == 1
