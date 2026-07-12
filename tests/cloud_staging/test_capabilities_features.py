"""GET /api/users/me/capabilities — ``features.protected_cloud`` gate (Task 13).

The capabilities payload gains a ``"features": {"protected_cloud": bool}``
key mirroring ``_is_protected_cloud_mode_enabled()`` in BOTH the admin and
non-admin response branches — the Cockpit toggle gate for the protected
cloud session-create checkbox reads this.

Follows the ExitStack pattern in tests/cloud_staging/test_apply_endpoints.py:
``import main`` (conftest puts orchestrator/ on sys.path), patch its module
globals, and call the endpoint coroutine directly.
"""

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

import main


def _patch_capabilities(
    *, user: dict, flag: bool, grants: dict | None = None
) -> ExitStack:
    stack = ExitStack()
    stack.enter_context(patch("main.require_approved_user", AsyncMock(return_value=user)))
    stack.enter_context(patch("main._is_protected_cloud_mode_enabled", lambda: flag))
    # _grant_project_ids -> user_visible_project_ids -> postgres_db; short-
    # circuit it so the non-admin branch doesn't need a real db.
    stack.enter_context(patch("main._grant_project_ids", AsyncMock(return_value=[])))
    stack.enter_context(
        patch(
            "services.grants_service.resolve_grants_for",
            AsyncMock(return_value=grants or {}),
        )
    )
    return stack


class TestCapabilitiesFeatures:
    @pytest.mark.asyncio
    async def test_admin_features_flag_on(self, user_admin, fake_request):
        stack = _patch_capabilities(user=user_admin, flag=True)
        with stack:
            result = await main.my_capabilities(fake_request)
        assert result["is_admin"] is True
        assert result["features"] == {"protected_cloud": True}

    @pytest.mark.asyncio
    async def test_admin_features_flag_off(self, user_admin, fake_request):
        stack = _patch_capabilities(user=user_admin, flag=False)
        with stack:
            result = await main.my_capabilities(fake_request)
        assert result["is_admin"] is True
        assert result["features"] == {"protected_cloud": False}

    @pytest.mark.asyncio
    async def test_non_admin_features_flag_on(self, user_a, fake_request):
        stack = _patch_capabilities(user=user_a, flag=True)
        with stack:
            result = await main.my_capabilities(fake_request)
        assert result["is_admin"] is False
        assert result["features"] == {"protected_cloud": True}

    @pytest.mark.asyncio
    async def test_non_admin_features_flag_off(self, user_a, fake_request):
        stack = _patch_capabilities(user=user_a, flag=False)
        with stack:
            result = await main.my_capabilities(fake_request)
        assert result["is_admin"] is False
        assert result["features"] == {"protected_cloud": False}
