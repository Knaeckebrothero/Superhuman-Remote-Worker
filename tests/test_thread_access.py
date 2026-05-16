"""G3 — multi-tenancy gate on the persistent-thread family.

Pre-G3, all 9 thread endpoints carried this inline check:

    if thread.get("user_id") and str(thread["user_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Not your thread")

It allowed any caller through when ``user_id IS NULL`` (orphan threads
left behind by user deletion) — letting attackers enumerate them by
UUID. It also didn't bypass for admins or enforce MCP project-scope
refusal. G3 replaces every occurrence with a single
``require_thread_owner`` call (new helper in ``security/access.py``).
A private ``_resolve_thread_for_forwarding`` helper that takes ``user``
but not ``request`` got an inline fail-closed + admin-bypass fix.

This file covers the helper directly (the cross-cutting properties) and
samples a few endpoints to prove the gate is wired. The previous F2-F7
pattern of patching ``main.require_approved_user`` AND
``security.access.require_approved_user`` is reused.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


# =============================================================================
# Patch helpers
# =============================================================================


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


def _scoped(user: dict, scope: str) -> dict:
    out = dict(user)
    out["scopes"] = [scope]
    out["auth_method"] = "mcp"
    return out


# =============================================================================
# require_thread_owner — the helper itself
# =============================================================================


class TestRequireThreadOwner:
    @pytest.mark.asyncio
    async def test_owner_passes(self, user_a, thread_a, fake_db, fake_request):
        from security.access import require_thread_owner

        with _patch_caller_and_db(user_a, fake_db):
            user, thread = await require_thread_owner(
                fake_request, fake_db, str(thread_a["id"])
            )
        assert thread is thread_a
        assert user["id"] == user_a["id"]

    @pytest.mark.asyncio
    async def test_cross_user_403(self, user_b, thread_a, fake_db, fake_request):
        from security.access import require_thread_owner

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await require_thread_owner(fake_request, fake_db, str(thread_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_orphan_thread_fail_closed(self, user_a, fake_db, fake_request):
        """G3 core fix: thread with user_id=NULL is no longer publicly readable."""
        from security.access import require_thread_owner

        orphan_id = "ccc55555-5555-5555-5555-555555555555"
        fake_db.get_thread = AsyncMock(
            return_value={"id": orphan_id, "user_id": None, "title": "ophan"}
        )
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await require_thread_owner(fake_request, fake_db, orphan_id)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_orphan_thread_admin_bypass(self, user_admin, fake_db, fake_request):
        """Admins still see orphans (they may need to clean them up)."""
        from security.access import require_thread_owner

        orphan_id = "ccc55555-5555-5555-5555-555555555555"
        orphan = {"id": orphan_id, "user_id": None, "title": "orphan"}
        fake_db.get_thread = AsyncMock(return_value=orphan)
        with _patch_caller_and_db(user_admin, fake_db):
            user, thread = await require_thread_owner(fake_request, fake_db, orphan_id)
        assert thread is orphan

    @pytest.mark.asyncio
    async def test_admin_bypass(self, user_admin, thread_a, fake_db, fake_request):
        from security.access import require_thread_owner

        with _patch_caller_and_db(user_admin, fake_db):
            user, thread = await require_thread_owner(
                fake_request, fake_db, str(thread_a["id"])
            )
        assert thread is thread_a

    @pytest.mark.asyncio
    async def test_missing_404(self, user_a, fake_db, fake_request):
        from security.access import require_thread_owner

        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await require_thread_owner(
                    fake_request, fake_db, "00000000-0000-0000-0000-000000000999"
                )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_mcp_project_scoped_token_rejected(
        self, user_a, thread_a, project_a, fake_db, fake_request
    ):
        """Threads have no project, so a project:<uuid> token can't read them."""
        from security.access import require_thread_owner

        scoped = _scoped(user_a, f"project:{project_a['id']}")
        with _patch_caller_and_db(scoped, fake_db):
            with pytest.raises(HTTPException) as exc:
                await require_thread_owner(fake_request, fake_db, str(thread_a["id"]))
        assert exc.value.status_code == 403


# =============================================================================
# Endpoint samples — gate is wired
# =============================================================================


class TestThreadEndpointGates:
    @pytest.mark.asyncio
    async def test_get_thread_orphan_blocked(self, user_a, fake_db, fake_request):
        from main import get_thread

        orphan_id = "ccc55555-5555-5555-5555-555555555555"
        fake_db.get_thread = AsyncMock(
            return_value={"id": orphan_id, "user_id": None, "title": "orphan"}
        )
        # _resolve_cloud_session_url should NOT be reached.
        sentinel = MagicMock(side_effect=AssertionError("called past gate"))
        with (
            _patch_caller_and_db(user_a, fake_db),
            patch("main._resolve_cloud_session_url", sentinel),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_thread(orphan_id, fake_request)
        assert exc.value.status_code == 403
        sentinel.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_thread_cross_user_blocked(
        self, user_b, thread_a, fake_db, fake_request
    ):
        from main import get_thread

        sentinel = MagicMock(side_effect=AssertionError("called past gate"))
        with (
            _patch_caller_and_db(user_b, fake_db),
            patch("main._resolve_cloud_session_url", sentinel),
        ):
            with pytest.raises(HTTPException) as exc:
                await get_thread(str(thread_a["id"]), fake_request)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_thread_owner_passes(
        self, user_a, thread_a, fake_db, fake_request
    ):
        from main import get_thread

        with (
            _patch_caller_and_db(user_a, fake_db),
            patch("main._resolve_cloud_session_url", MagicMock(return_value=None)),
        ):
            result = await get_thread(str(thread_a["id"]), fake_request)
        assert result["id"] == thread_a["id"]

    @pytest.mark.asyncio
    async def test_get_thread_messages_history_cross_user_blocked(
        self, user_b, thread_a, fake_db, fake_request
    ):
        from main import get_thread_messages_history

        fake_db.get_thread_messages_history = AsyncMock(
            side_effect=AssertionError("called past gate")
        )
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_thread_messages_history(
                    str(thread_a["id"]), fake_request, limit=200, offset=0
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_thread_event_stream_orphan_blocked(
        self, user_a, fake_db, fake_request
    ):
        from main import thread_event_stream

        orphan_id = "ccc55555-5555-5555-5555-555555555555"
        fake_db.get_thread = AsyncMock(
            return_value={
                "id": orphan_id,
                "user_id": None,
                "events_epoch": 0,
                "title": "x",
            }
        )
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await thread_event_stream(orphan_id, fake_request)
        assert exc.value.status_code == 403
