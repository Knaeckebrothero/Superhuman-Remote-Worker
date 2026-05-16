"""F2 — multi-tenancy gates on the builder session endpoints.

Covers the five builder endpoints in orchestrator/main.py:
    POST   /api/builder/sessions
    GET    /api/builder/sessions
    GET    /api/builder/sessions/{id}
    GET    /api/builder/sessions/{id}/messages
    POST   /api/builder/sessions/{id}/message

The shared 3-user / 2-project fixture (`user_a`, `user_b`, `user_admin`,
`fake_db`, ...) is defined in ``conftest.py``. Endpoint handlers are
imported from ``main`` and called directly — the patches replace both
the main-module copy of ``require_approved_user`` (used by handlers'
inline call) and the access-module copy (used by the F1 dependencies).
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException


# =============================================================================
# Patch helpers
# =============================================================================


def _patch_caller_and_db(user: dict, db):
    """Stack the patches every endpoint test needs.

    The handlers call ``require_approved_user`` via their main-module
    import; ``require_*`` dependencies inside ``security.access`` call
    the access-module copy. Both must be replaced. ``postgres_db`` is
    the module-global the handlers reach for.
    """
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


# =============================================================================
# create_builder_session — body.user_id is ignored, caller.id is forced
# =============================================================================


class TestCreateBuilderSession:
    @pytest.mark.asyncio
    async def test_uses_caller_id_when_body_user_id_is_none(
        self, user_a, fake_db, fake_request
    ):
        from main import BuilderSessionCreate, create_builder_session

        fake_db.create_builder_session = AsyncMock(return_value={"id": "new"})
        with _patch_caller_and_db(user_a, fake_db):
            await create_builder_session(
                fake_request, BuilderSessionCreate(expert_id=None, user_id=None)
            )
        fake_db.create_builder_session.assert_awaited_once_with(
            expert_id=None, user_id=str(user_a["id"])
        )

    @pytest.mark.asyncio
    async def test_body_user_id_is_ignored_when_set_to_other(
        self, user_a, user_b, fake_db, fake_request
    ):
        """Self-impersonation guard: body.user_id can't redirect ownership."""
        from main import BuilderSessionCreate, create_builder_session

        fake_db.create_builder_session = AsyncMock(return_value={"id": "new"})
        with _patch_caller_and_db(user_a, fake_db):
            await create_builder_session(
                fake_request,
                BuilderSessionCreate(expert_id=None, user_id=str(user_b["id"])),
            )
        # Even though the body asked for user_b, the DB call ignores it.
        fake_db.create_builder_session.assert_awaited_once_with(
            expert_id=None, user_id=str(user_a["id"])
        )


# =============================================================================
# list_builder_sessions — caller-scoped, admin override allowed
# =============================================================================


class TestListBuilderSessions:
    @pytest.mark.asyncio
    async def test_no_query_param_lists_own_sessions(
        self, user_a, fake_db, fake_request
    ):
        from main import list_builder_sessions

        fake_db.list_builder_sessions = AsyncMock(return_value=[{"id": "s1"}])
        with _patch_caller_and_db(user_a, fake_db):
            result = await list_builder_sessions(fake_request, user_id=None)
        fake_db.list_builder_sessions.assert_awaited_once_with(str(user_a["id"]))
        assert result == [{"id": "s1"}]

    @pytest.mark.asyncio
    async def test_query_param_matching_caller_passes(
        self, user_a, fake_db, fake_request
    ):
        from main import list_builder_sessions

        fake_db.list_builder_sessions = AsyncMock(return_value=[])
        with _patch_caller_and_db(user_a, fake_db):
            await list_builder_sessions(fake_request, user_id=str(user_a["id"]))
        fake_db.list_builder_sessions.assert_awaited_once_with(str(user_a["id"]))

    @pytest.mark.asyncio
    async def test_cross_user_query_param_403_for_non_admin(
        self, user_a, user_b, fake_db, fake_request
    ):
        from main import list_builder_sessions

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await list_builder_sessions(fake_request, user_id=str(user_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_list_other_user(
        self, user_admin, user_a, fake_db, fake_request
    ):
        from main import list_builder_sessions

        fake_db.list_builder_sessions = AsyncMock(return_value=[{"id": "x"}])
        with _patch_caller_and_db(user_admin, fake_db):
            result = await list_builder_sessions(
                fake_request, user_id=str(user_a["id"])
            )
        fake_db.list_builder_sessions.assert_awaited_once_with(str(user_a["id"]))
        assert result == [{"id": "x"}]


# =============================================================================
# get_builder_session — owner / admin only
# =============================================================================


class TestGetBuilderSession:
    @pytest.mark.asyncio
    async def test_owner_passes(self, user_a, builder_session_a, fake_db, fake_request):
        from main import get_builder_session

        with _patch_caller_and_db(user_a, fake_db):
            result = await get_builder_session(
                fake_request, str(builder_session_a["id"])
            )
        assert result is builder_session_a

    @pytest.mark.asyncio
    async def test_other_user_403(
        self, user_b, builder_session_a, fake_db, fake_request
    ):
        from main import get_builder_session

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_builder_session(fake_request, str(builder_session_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_404(self, user_a, fake_db, fake_request):
        from main import get_builder_session

        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_builder_session(
                    fake_request, "ffffffff-ffff-ffff-ffff-ffffffffffff"
                )
        assert exc.value.status_code == 404


# =============================================================================
# get_builder_messages — owner / admin only
# =============================================================================


class TestGetBuilderMessages:
    @pytest.mark.asyncio
    async def test_owner_passes(self, user_a, builder_session_a, fake_db, fake_request):
        from main import get_builder_messages

        fake_db.get_builder_messages = AsyncMock(return_value=[{"role": "user"}])
        with _patch_caller_and_db(user_a, fake_db):
            result = await get_builder_messages(
                fake_request, str(builder_session_a["id"])
            )
        assert result == [{"role": "user"}]

    @pytest.mark.asyncio
    async def test_other_user_403_without_reading_messages(
        self, user_b, builder_session_a, fake_db, fake_request
    ):
        """Gate fires before any message read, so the DB call must not happen."""
        from main import get_builder_messages

        fake_db.get_builder_messages = AsyncMock()
        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_builder_messages(fake_request, str(builder_session_a["id"]))
        assert exc.value.status_code == 403
        fake_db.get_builder_messages.assert_not_awaited()


# =============================================================================
# send_builder_message — gate + active_* validation
# =============================================================================
#
# The endpoint streams via SSE on the happy path; for these tests we only
# care about the synchronous gating that happens before the StreamingResponse
# is constructed. A 403 raised in the gate path propagates out before any
# stream work begins, so we don't need to consume the response.


class TestSendBuilderMessageGate:
    @pytest.mark.asyncio
    async def test_other_user_403(
        self, user_b, builder_session_a, fake_db, fake_request
    ):
        from main import BuilderMessageRequest, send_builder_message

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await send_builder_message(
                    fake_request,
                    str(builder_session_a["id"]),
                    BuilderMessageRequest(message="hi"),
                )
        assert exc.value.status_code == 403


class TestSendBuilderMessageActiveJobValidation:
    @pytest.mark.asyncio
    async def test_active_job_in_caller_project_passes_gate(
        self, user_a, builder_session_a, job_a, fake_db, fake_request
    ):
        """user_a owns session_a AND job_a (in project_a) → both gates pass."""
        from main import BuilderMessageRequest, send_builder_message

        # Stub out everything after the gate so we don't run the actual stream.
        fake_db.create_builder_message = AsyncMock()
        fake_db.get_builder_messages = AsyncMock(return_value=[])
        with _patch_caller_and_db(user_a, fake_db):
            with patch("main.get_builder_model", AsyncMock(return_value=None)):
                # No default model configured → 503 *after* gates pass. That
                # confirms gates passed. Anything other than 403/404 is fine.
                with pytest.raises(HTTPException) as exc:
                    await send_builder_message(
                        fake_request,
                        str(builder_session_a["id"]),
                        BuilderMessageRequest(
                            message="hi", active_job_id=str(job_a["id"])
                        ),
                    )
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_active_job_other_user_403(
        self, user_a, builder_session_a, job_b, fake_db, fake_request
    ):
        """user_a owns session_a but NOT job_b — gate must reject."""
        from main import BuilderMessageRequest, send_builder_message

        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await send_builder_message(
                    fake_request,
                    str(builder_session_a["id"]),
                    BuilderMessageRequest(message="hi", active_job_id=str(job_b["id"])),
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_active_project_other_user_403(
        self, user_a, builder_session_a, project_b, fake_db, fake_request
    ):
        """user_a owns session_a but is not a member of project_b — gate rejects."""
        from main import BuilderMessageRequest, send_builder_message

        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await send_builder_message(
                    fake_request,
                    str(builder_session_a["id"]),
                    BuilderMessageRequest(
                        message="hi", active_project_id=str(project_b["id"])
                    ),
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_active_job_404_passes_through(
        self, user_a, builder_session_a, fake_db, fake_request
    ):
        """A bogus active_job_id surfaces as 404 from require_job_access."""
        from main import BuilderMessageRequest, send_builder_message

        bogus = UUID("deaddead-dead-dead-dead-deaddeaddead")
        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await send_builder_message(
                    fake_request,
                    str(builder_session_a["id"]),
                    BuilderMessageRequest(message="hi", active_job_id=str(bogus)),
                )
        assert exc.value.status_code == 404
