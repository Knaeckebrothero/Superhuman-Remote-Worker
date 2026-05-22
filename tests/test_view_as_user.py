"""Tests for the admin 'View as user' shadow mechanism.

The shadow lives in ``security.auth.require_approved_user``: an admin
request carrying ``X-Admin-View-As: user`` gets a user dict with
``is_admin=False`` so the visibility helpers narrow as if the caller
were a regular user. ``real_is_admin`` is preserved so admin-only gates
(``_require_admin``) keep working.

Coverage matrix (per design doc § PR 1):
  (a) admin without header → admin-mode response
  (b) admin with header → user-mode response (is_admin shadowed)
  (c) non-admin with header → header ignored
  (d) admin-only endpoint with header → still 200 (uses real_is_admin)

See docs/features/admin_view_as_user.md.
"""

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from security import auth


# =============================================================================
# Helpers
# =============================================================================
#
# require_approved_user reads ``request.headers.get(VIEW_AS_HEADER, "")``;
# a bare MagicMock would return a MagicMock for ``.get``, which compares
# unequal to every string and silently passes the wrong branch. So we
# stub headers with a real dict.


def _request(view_as_header: str | None = None) -> MagicMock:
    """Minimal Request stub with optional ``X-Admin-View-As`` value."""
    req = MagicMock()
    headers: dict[str, str] = {}
    if view_as_header is not None:
        headers[auth.VIEW_AS_HEADER] = view_as_header
    req.headers = headers
    return req


def _patch_current_user(user: dict):
    """Make ``auth.get_current_user`` return ``user`` for the test scope."""
    return patch.object(auth, "get_current_user", AsyncMock(return_value=user))


@pytest.fixture
def admin_user() -> dict:
    return {
        "id": UUID("33333333-3333-3333-3333-333333333333"),
        "display_name": "admin",
        "is_admin": True,
        "is_approved": True,
        "scopes": [],
    }


@pytest.fixture
def regular_user() -> dict:
    return {
        "id": UUID("11111111-1111-1111-1111-111111111111"),
        "display_name": "user_a",
        "is_admin": False,
        "is_approved": True,
        "scopes": [],
    }


# =============================================================================
# require_approved_user — header interpretation
# =============================================================================


class TestRequireApprovedUserShadow:
    @pytest.mark.asyncio
    async def test_admin_without_header_keeps_admin(self, admin_user):
        """Case (a): admin, no header → no shadow."""
        with _patch_current_user(admin_user):
            user = await auth.require_approved_user(_request(), MagicMock())
        assert user["is_admin"] is True
        assert user["real_is_admin"] is True

    @pytest.mark.asyncio
    async def test_admin_with_header_is_shadowed(self, admin_user):
        """Case (b): admin, header=user → shadow flips is_admin off."""
        with _patch_current_user(admin_user):
            user = await auth.require_approved_user(_request("user"), MagicMock())
        assert user["is_admin"] is False
        assert user["real_is_admin"] is True

    @pytest.mark.asyncio
    async def test_admin_with_uppercase_header_is_shadowed(self, admin_user):
        """Header match is case-insensitive (``.lower()`` normalization)."""
        with _patch_current_user(admin_user):
            user = await auth.require_approved_user(_request("USER"), MagicMock())
        assert user["is_admin"] is False
        assert user["real_is_admin"] is True

    @pytest.mark.asyncio
    async def test_admin_with_unknown_header_value_is_noop(self, admin_user):
        """Anything other than ``"user"`` is ignored — fail-open to admin.

        Guards against future header values (``user:<uuid>``, ``all``) being
        accidentally interpreted as the v1 shadow.
        """
        with _patch_current_user(admin_user):
            user = await auth.require_approved_user(_request("all"), MagicMock())
        assert user["is_admin"] is True
        assert user["real_is_admin"] is True

    @pytest.mark.asyncio
    async def test_admin_with_future_user_uuid_header_is_noop(self, admin_user):
        """``user:<uuid>`` is reserved for the impersonation follow-up; v1
        treats it as an unknown value and falls through to no-shadow."""
        with _patch_current_user(admin_user):
            user = await auth.require_approved_user(
                _request("user:11111111-1111-1111-1111-111111111111"), MagicMock()
            )
        assert user["is_admin"] is True
        assert user["real_is_admin"] is True

    @pytest.mark.asyncio
    async def test_non_admin_with_header_unchanged(self, regular_user):
        """Case (c): non-admin sending the header has no effect."""
        with _patch_current_user(regular_user):
            user = await auth.require_approved_user(_request("user"), MagicMock())
        assert user["is_admin"] is False
        assert user["real_is_admin"] is False

    @pytest.mark.asyncio
    async def test_non_admin_without_header_has_real_is_admin_false(self, regular_user):
        """``real_is_admin`` is always set for parity with the admin path."""
        with _patch_current_user(regular_user):
            user = await auth.require_approved_user(_request(), MagicMock())
        assert user["real_is_admin"] is False

    @pytest.mark.asyncio
    async def test_unapproved_user_still_403(self):
        """The shadow runs *after* the approved check — header can't bypass."""
        unapproved = {
            "id": UUID("99999999-9999-9999-9999-999999999999"),
            "is_admin": False,
            "is_approved": False,
        }
        with _patch_current_user(unapproved):
            with pytest.raises(HTTPException) as exc:
                await auth.require_approved_user(_request("user"), MagicMock())
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_returned_dict_is_not_the_input(self, admin_user):
        """Mutating the returned dict must not bleed into the cached one."""
        with _patch_current_user(admin_user):
            user = await auth.require_approved_user(_request("user"), MagicMock())
        assert user is not admin_user
        # The source dict stays admin-true (no in-place mutation).
        assert admin_user["is_admin"] is True


# =============================================================================
# _require_admin — admin-only gate respects real_is_admin
# =============================================================================
#
# ``_require_admin`` lives in main.py and would normally need the full
# orchestrator import graph. Instead we exercise the contract: any caller
# that checks ``real_is_admin`` on the returned dict — which is exactly
# what _require_admin does — sees the un-shadowed flag.


class TestRequireAdminContract:
    @pytest.mark.asyncio
    async def test_admin_in_shadow_keeps_real_is_admin_true(self, admin_user):
        """Case (d): admin-only endpoint must still pass while shadowed."""
        with _patch_current_user(admin_user):
            user = await auth.require_approved_user(_request("user"), MagicMock())
        # _require_admin's gate: `if not user.get("real_is_admin", False): 403`
        assert user.get("real_is_admin") is True

    @pytest.mark.asyncio
    async def test_regular_user_fails_real_is_admin_check(self, regular_user):
        with _patch_current_user(regular_user):
            user = await auth.require_approved_user(_request("user"), MagicMock())
        assert user.get("real_is_admin") is False


# =============================================================================
# Inline admin privilege gates inside otherwise non-admin endpoints
# =============================================================================
#
# Some endpoints are not fully admin-only but contain an inline check that
# escalates privileges for specific scopes:
#   - ``create_mcp_token`` (main.py): scope ``"all"`` requires admin
#   - ``create_api_key`` (main.py):   scope ``"admin"`` requires admin
#
# These gates MUST consult ``real_is_admin`` — otherwise an admin in
# "View as user" mode gets a 403 when trying to exercise their privilege
# (regression on dev cluster 2026-05-22, cockpit silently swallowed the
# 403 and the user saw "nothing happened" on click).
#
# The endpoints live behind orchestrator/main.py's heavy import graph, so
# we don't exercise them via TestClient here. Two complementary tests:
#   1. Contract: ``require_approved_user`` exposes ``real_is_admin`` so
#      a gate using it passes for shadowed admins.
#   2. Source-pattern: the two specific gates in main.py reference
#      ``real_is_admin``, not the shadowed ``is_admin``.


class TestInlinePrivilegeGates:
    """Inline admin gates inside non-admin endpoints must use real_is_admin."""

    @pytest.mark.asyncio
    async def test_admin_in_shadow_passes_real_is_admin_gate(self, admin_user):
        """An admin with view-as=user header must still satisfy a
        ``not user.get("real_is_admin", False)`` privilege check.
        """
        with _patch_current_user(admin_user):
            user = await auth.require_approved_user(_request("user"), MagicMock())

        privileged_scope = True  # e.g. scope == "all" or "admin" in scopes
        blocked = privileged_scope and not user.get("real_is_admin", False)
        assert not blocked, (
            "Admin in view-as mode must keep privileged scopes — "
            "inline gates must check real_is_admin, not is_admin"
        )

    def test_create_mcp_token_admin_gate_uses_real_is_admin(self):
        """``create_mcp_token`` scope=='all' check must use real_is_admin.

        Source-pattern guard. If someone reverts the gate to ``is_admin``
        the dev-cluster regression (admin in view-as=user can't create
        full-access tokens) returns silently. This test catches that.
        """
        main_py = (
            pathlib.Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "main.py"
        ).read_text()

        # Locate the endpoint body
        marker = "async def create_mcp_token("
        assert marker in main_py, "Endpoint signature changed — update test"
        body = main_py[main_py.index(marker):]
        # The privilege gate sits within the first ~50 lines
        gate_window = body[: body.index("\n@app.")]

        gate_line = next(
            (line for line in gate_window.splitlines() if 'scope == "all"' in line),
            None,
        )
        assert gate_line is not None, (
            "Did not find the 'scope == \"all\"' privilege gate"
        )
        assert "real_is_admin" in gate_line, (
            f"Privilege gate must check real_is_admin, not is_admin "
            f"(otherwise admins in view-as mode get a false 403). "
            f"Found: {gate_line.strip()!r}"
        )

    def test_create_api_key_admin_gate_uses_real_is_admin(self):
        """``create_api_key`` 'admin' scope check must use real_is_admin.

        Same bug pattern as create_mcp_token — kept as a separate test so
        a partial revert (one fixed, one not) is caught.
        """
        main_py = (
            pathlib.Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "main.py"
        ).read_text()

        marker = "async def create_api_key("
        assert marker in main_py, "Endpoint signature changed — update test"
        body = main_py[main_py.index(marker):]
        gate_window = body[: body.index("\n@app.")]

        gate_line = next(
            (line for line in gate_window.splitlines() if '"admin" in requested' in line),
            None,
        )
        assert gate_line is not None, (
            "Did not find the '\"admin\" in requested' privilege gate"
        )
        assert "real_is_admin" in gate_line, (
            f"PAT admin-scope gate must check real_is_admin, not is_admin. "
            f"Found: {gate_line.strip()!r}"
        )
