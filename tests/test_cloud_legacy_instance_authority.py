"""Pre-0186 rows must not take the project page down.

Migration 0186 added ``main_cloud_backend_instance_id`` nullable, with no
backfill, so every row stamped with a provider name *before* that migration
carries a provider but no installation authority. ``MainCloudRouter.for_project``
fails closed on exactly that shape — correctly, because acting on a guessed
installation is unrecoverable.

The bug was routing a *decorative* read through that fail-closed seam:
``get_project`` resolved a backend only to build one optional deep-link URL, so
an unstamped row 500'd the whole endpoint. The cockpit's project guard
swallows every HTTP error into ``null`` and reports the single message it has,
so five live projects on the dev cluster read as "You don't have access to that
project" while the caller was in fact the owner.

These tests pin both halves of the split:

* decorative callers degrade to ``None`` (``for_project_optional``),
* effect callers keep raising (``for_project``) — a legacy row must never be
  granted, shared, or deleted against a guessed installation.
"""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from services.cloud import MainCloudRouter
from services.cloud.errors import FeatureNotAvailable


INSTANCE_ID = "4e72e665-1f70-4b69-9804-d981b51416e6"


def _router() -> MainCloudRouter:
    """Router whose active backend is *not* registered under any instance id."""
    active = MagicMock()
    active.backend_id = "nextcloud"
    active.backend_instance_id = None
    return MainCloudRouter(active)


def _legacy_row() -> dict:
    """A pre-0186 row: provider stamped, installation authority absent."""
    return {
        "id": str(uuid4()),
        "main_cloud_backend": "nextcloud",
        "main_cloud_backend_instance_id": None,
        "main_cloud_folder_handle": "nextcloud:12345",
    }


# =============================================================================
# Router — the decorative/effect split
# =============================================================================


class TestForProjectOptional:
    def test_legacy_row_degrades_to_none(self):
        assert _router().for_project_optional(_legacy_row()) is None

    def test_legacy_row_still_raises_on_the_effect_path(self):
        """The raising form is load-bearing: do not 'fix' it with a fallback.

        Falling back to the active backend here would let a member grant or a
        folder delete land on whichever installation happens to be active,
        which is precisely the unrecoverable outcome 0186 exists to prevent.
        """
        with pytest.raises(FeatureNotAvailable):
            _router().for_project(_legacy_row())

    def test_uncached_instance_degrades_to_none(self):
        """A stamped row whose instance this replica never cached is decorative-safe."""
        row = _legacy_row()
        row["main_cloud_backend_instance_id"] = INSTANCE_ID
        assert _router().for_project_optional(row) is None
        with pytest.raises(FeatureNotAvailable):
            _router().for_project(row)

    def test_fully_unstamped_row_resolves_active(self):
        """Both columns NULL is the ordinary pre-cloud project — not legacy."""
        router = _router()
        row = {"id": str(uuid4()), "main_cloud_backend": None}
        assert router.for_project_optional(row) is router.active
        assert router.for_project(row) is router.active

    def test_thread_variant_matches(self):
        router = _router()
        row = {
            "id": str(uuid4()),
            "main_cloud_backend": "nextcloud",
            "main_cloud_backend_instance_id": None,
        }
        assert router.for_thread_optional(row) is None
        with pytest.raises(FeatureNotAvailable):
            router.for_thread(row)


# =============================================================================
# get_project — the reported regression
# =============================================================================


class TestGetProjectOnLegacyRow:
    @pytest.mark.asyncio
    async def test_owner_reads_legacy_project_without_500(
        self, user_a, project_a, fake_db, fake_request
    ):
        """The exact dev-cluster shape: owner, cloud folder, no instance id."""
        project_a["main_cloud_backend"] = "nextcloud"
        project_a["main_cloud_backend_instance_id"] = None
        project_a["main_cloud_folder_handle"] = "nextcloud:12345"

        from main import get_project

        real_router = _router()
        with (
            patch("main.require_approved_user", AsyncMock(return_value=user_a)),
            patch(
                "security.access.require_approved_user",
                AsyncMock(return_value=user_a),
            ),
            patch("main.postgres_db", fake_db),
            patch(
                "main._ensure_project_cloud_resources",
                AsyncMock(side_effect=lambda p: p),
            ),
            patch("main.main_cloud_router", real_router),
        ):
            result = await get_project(fake_request, str(project_a["id"]))

        assert result["id"] == project_a["id"]
        # The deep-link is the one thing that degrades; the cockpit hides the
        # button on None.
        assert result["cloud_storage_url"] is None


# =============================================================================
# Mutations — refuse, but legibly, and without half-landing
# =============================================================================


class TestAddMemberOnLegacyRow:
    @pytest.mark.asyncio
    async def test_refuses_409_before_writing_the_member_row(
        self, user_a, user_b, project_a, fake_db, fake_request
    ):
        """A member we cannot sync to the cloud must not be written at all.

        The cloud group add is load-bearing — a member without it cannot reach
        the project's files — so writing the row and then failing the sync
        would report a 500 for a member that nonetheless exists.
        """
        project_a["main_cloud_backend"] = "nextcloud"
        project_a["main_cloud_backend_instance_id"] = None

        from main import ProjectMemberAdd, add_project_member

        fake_db.add_project_member = AsyncMock(
            side_effect=AssertionError("member row written despite refusal")
        )
        with (
            patch(
                "main.require_project_owner",
                AsyncMock(return_value=(user_a, project_a)),
            ),
            patch("main.postgres_db", fake_db),
            patch("main.main_cloud_router", _router()),
        ):
            with pytest.raises(HTTPException) as exc:
                await add_project_member(
                    str(project_a["id"]),
                    ProjectMemberAdd(user_id=str(user_b["id"]), role="editor"),
                    fake_request,
                )

        assert exc.value.status_code == 409
        assert "backfill" in exc.value.detail
        fake_db.add_project_member.assert_not_awaited()
