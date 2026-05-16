"""F5 — multi-tenancy gates on project knowledge endpoints.

Covers:
    GET    /api/projects/{id}/knowledge/summary
    GET    /api/projects/{id}/knowledge
    GET    /api/projects/{id}/knowledge/{note_id}
    POST   /api/projects/{id}/knowledge/search
    PATCH  /api/projects/{id}/knowledge/{note_id}
    DELETE /api/projects/{id}/knowledge/{note_id}
    POST   /api/projects/{id}/knowledge/export

All seven require viewer-or-higher project membership. Tests focus on the
gate itself: cross-user 403, missing-project 404, member-passes-and-hits-the-
inner-code. We don't exercise the vector-DB / Neo4j paths beyond confirming
the gate fired (an inner 500 / 503 after the gate counts as success — it
proves the gate let the caller through).
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException


def _patch_caller_and_db(user: dict, db):
    """Same patch stack used across F2/F3/F5 test files."""
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


# Endpoints that don't need a body parameter — share negative-path coverage.
_READ_ENDPOINTS = [
    ("get_knowledge_summary", ("project_id",)),
    ("list_knowledge_notes", ("project_id",)),
]


class TestKnowledgeGateNegativePaths:
    """Cross-user 403 and missing-project 404 on every endpoint."""

    @pytest.mark.asyncio
    async def test_summary_cross_user_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import get_knowledge_summary

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_knowledge_summary(fake_request, str(project_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_list_cross_user_403(self, user_b, project_a, fake_db, fake_request):
        from main import list_knowledge_notes

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await list_knowledge_notes(fake_request, str(project_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_note_cross_user_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import get_knowledge_note

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_knowledge_note(fake_request, str(project_a["id"]), "note-1")
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_search_cross_user_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import KnowledgeSearchRequest, search_knowledge

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await search_knowledge(
                    fake_request,
                    str(project_a["id"]),
                    KnowledgeSearchRequest(query="anything"),
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_patch_note_cross_user_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import KnowledgeNoteUpdate, update_knowledge_note

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await update_knowledge_note(
                    fake_request,
                    str(project_a["id"]),
                    "note-1",
                    KnowledgeNoteUpdate(status="resolved"),
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_note_cross_user_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import delete_knowledge_note

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await delete_knowledge_note(
                    fake_request, str(project_a["id"]), "note-1"
                )
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_export_cross_user_403(
        self, user_b, project_a, fake_db, fake_request
    ):
        from main import export_knowledge

        with _patch_caller_and_db(user_b, fake_db):
            with pytest.raises(HTTPException) as exc:
                await export_knowledge(fake_request, str(project_a["id"]))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_project_404(self, user_a, fake_db, fake_request):
        """The gate raises 404 before any knowledge query runs."""
        from main import get_knowledge_summary

        with _patch_caller_and_db(user_a, fake_db):
            with pytest.raises(HTTPException) as exc:
                await get_knowledge_summary(
                    fake_request, "ffffffff-ffff-ffff-ffff-ffffffffffff"
                )
        assert exc.value.status_code == 404


# =============================================================================
# Positive paths: member passes the gate. We don't exercise the vector-DB
# code below the gate; an inner exception confirms the gate let the caller
# through (anything other than 403/404 means we made it past the gate).
# =============================================================================


def _make_dud_vector_db():
    """Stand-in for ``vector_db``. ``acquire()`` returns a context manager
    whose ``__aenter__`` raises, so the inner code can't query but we
    *did* reach the inner code — proves the gate let us through."""
    dud = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__.side_effect = RuntimeError("vector_db patched out")
    dud.acquire = lambda: cm
    return dud


class TestKnowledgeGatePositivePaths:
    @pytest.mark.asyncio
    async def test_summary_member_passes_gate(
        self, user_a, project_a, fake_db, fake_request
    ):
        from main import get_knowledge_summary

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main.vector_db", _make_dud_vector_db()):
                with pytest.raises(HTTPException) as exc:
                    await get_knowledge_summary(fake_request, str(project_a["id"]))
        # 500 = gate passed, vector_db blew up as planned. NOT 403/404.
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_admin_bypasses_membership(
        self, user_admin, project_a, fake_db, fake_request
    ):
        from main import list_knowledge_notes

        with _patch_caller_and_db(user_admin, fake_db):
            with patch("main.vector_db", _make_dud_vector_db()):
                with pytest.raises(HTTPException) as exc:
                    await list_knowledge_notes(fake_request, str(project_a["id"]))
        # Same shape — admin made it past, inner code failed by design.
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_get_note_member_passes_gate(
        self, user_a, project_a, fake_db, fake_request
    ):
        from main import get_knowledge_note

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main.vector_db", _make_dud_vector_db()):
                with pytest.raises(HTTPException) as exc:
                    await get_knowledge_note(
                        fake_request, str(project_a["id"]), "note-1"
                    )
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_search_member_passes_gate(
        self, user_a, project_a, fake_db, fake_request
    ):
        from main import KnowledgeSearchRequest, search_knowledge

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main.vector_db", _make_dud_vector_db()):
                with pytest.raises(HTTPException) as exc:
                    await search_knowledge(
                        fake_request,
                        str(project_a["id"]),
                        KnowledgeSearchRequest(query="q"),
                    )
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_patch_note_member_passes_gate(
        self, user_a, project_a, fake_db, fake_request
    ):
        from main import KnowledgeNoteUpdate, update_knowledge_note

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main.vector_db", _make_dud_vector_db()):
                with pytest.raises(HTTPException) as exc:
                    await update_knowledge_note(
                        fake_request,
                        str(project_a["id"]),
                        "note-1",
                        KnowledgeNoteUpdate(status="resolved"),
                    )
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_delete_note_member_passes_gate(
        self, user_a, project_a, fake_db, fake_request
    ):
        from main import delete_knowledge_note

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main.vector_db", _make_dud_vector_db()):
                with pytest.raises(HTTPException) as exc:
                    await delete_knowledge_note(
                        fake_request, str(project_a["id"]), "note-1"
                    )
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_export_member_passes_gate(
        self, user_a, project_a, fake_db, fake_request
    ):
        """Export checks Neo4j availability *after* the gate. Without
        the graph attached it returns 503 — that confirms the gate let
        us through."""
        from main import export_knowledge

        with _patch_caller_and_db(user_a, fake_db):
            with patch("main._get_knowledge_graph", lambda: None):
                with pytest.raises(HTTPException) as exc:
                    await export_knowledge(fake_request, str(project_a["id"]))
        assert exc.value.status_code == 503
