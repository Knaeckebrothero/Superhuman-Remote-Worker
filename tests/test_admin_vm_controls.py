"""Tests for the admin VM workspace controls.

Covers:
- Route registration of the new admin endpoints.
- Pydantic model shape for `AdminUserUpdate`.
- The `_check_vm_permission` gate — kill-switch + per-user grant + admin bypass.

Full TestClient integration with DB + Keycloak is out of scope; we mock
`postgres_db` directly to exercise the gate helper on its own. End-to-end
coverage of the submit/dispatch paths is verified manually per the plan.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

_ORCH = Path(__file__).parent.parent / "orchestrator"
if str(_ORCH) not in sys.path:
    sys.path.insert(0, str(_ORCH))

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import main as orch_main  # noqa: E402
from main import AdminUserUpdate, app  # noqa: E402

MODULE = "main"


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


ADMIN_VM_ROUTES = {
    ("GET", "/api/admin/system-settings/vm_workspaces"),
    ("PUT", "/api/admin/system-settings/vm_workspaces"),
    ("GET", "/api/admin/users"),
    ("PATCH", "/api/admin/users/{user_id}"),
}


def _registered_routes() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", "")
        for m in methods:
            out.add((m, path))
    return out


class TestAdminVmRoutesRegistered:
    def test_every_admin_vm_route_is_wired(self):
        registered = _registered_routes()
        missing = [r for r in ADMIN_VM_ROUTES if r not in registered]
        assert not missing, f"missing admin VM routes: {missing}"


# ---------------------------------------------------------------------------
# Pydantic body
# ---------------------------------------------------------------------------


class TestAdminUserUpdate:
    def test_accepts_partial_is_admin_only(self):
        body = AdminUserUpdate(is_admin=True)
        assert body.is_admin is True
        assert body.can_use_vm is None

    def test_accepts_partial_can_use_vm_only(self):
        body = AdminUserUpdate(can_use_vm=True)
        assert body.can_use_vm is True
        assert body.is_admin is None

    def test_accepts_empty_body(self):
        body = AdminUserUpdate()
        assert body.is_admin is None
        assert body.can_use_vm is None


# ---------------------------------------------------------------------------
# _check_vm_permission gate
# ---------------------------------------------------------------------------


def _patch_db(*, setting=None, setting_error=None):
    """Patch ``main.postgres_db`` for the gate tests.

    The gate makes two async DB calls: ``get_system_setting`` (the global
    kill-switch) and ``user_can_use_vm`` (the per-user grant). The latter
    resolves capability grants and falls back to ``bool(user['can_use_vm'])``
    when none exist, so the mock mirrors that fallback — the per-user
    expectations below read straight off the user dict, as before.
    """
    mock_db = MagicMock()
    mock_db.get_system_setting = AsyncMock(
        side_effect=setting_error, return_value=setting
    )
    mock_db.user_can_use_vm = AsyncMock(
        side_effect=lambda u: bool(u and u.get("can_use_vm"))
    )
    return patch(f"{MODULE}.postgres_db", mock_db)


class TestCheckVmPermission:
    @pytest.mark.asyncio
    async def test_no_op_when_job_does_not_need_vm(self):
        """The gate short-circuits for non-VM jobs — no DB call, no raise."""
        with _patch_db(setting_error=AssertionError("should not read kill-switch")):
            await orch_main._check_vm_permission(user=None, job_needs_vm=False)

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_admin(self):
        admin = {"id": "u1", "is_admin": True, "can_use_vm": True}
        with _patch_db(setting={"value": {"enabled": False}}):
            with pytest.raises(HTTPException) as exc:
                await orch_main._check_vm_permission(admin, job_needs_vm=True)
            assert exc.value.status_code == 403
            assert "globally disabled" in exc.value.detail

    @pytest.mark.asyncio
    async def test_kill_switch_blocks_granted_non_admin(self):
        user = {"id": "u2", "is_admin": False, "can_use_vm": True}
        with _patch_db(setting={"value": {"enabled": False}}):
            with pytest.raises(HTTPException) as exc:
                await orch_main._check_vm_permission(user, job_needs_vm=True)
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_bypasses_per_user_grant(self):
        """Admin without can_use_vm still gets through when kill-switch is on."""
        admin = {"id": "u3", "is_admin": True, "can_use_vm": False}
        with _patch_db(setting={"value": {"enabled": True}}):
            await orch_main._check_vm_permission(admin, job_needs_vm=True)

    @pytest.mark.asyncio
    async def test_non_admin_without_grant_denied(self):
        user = {"id": "u4", "is_admin": False, "can_use_vm": False}
        with _patch_db(setting=None):
            with pytest.raises(HTTPException) as exc:
                await orch_main._check_vm_permission(user, job_needs_vm=True)
            assert exc.value.status_code == 403
            assert "not permitted" in exc.value.detail

    @pytest.mark.asyncio
    async def test_non_admin_with_grant_allowed(self):
        user = {"id": "u5", "is_admin": False, "can_use_vm": True}
        with _patch_db(setting=None):
            await orch_main._check_vm_permission(user, job_needs_vm=True)

    @pytest.mark.asyncio
    async def test_missing_user_denied(self):
        """No user record = treated as unauthenticated non-admin."""
        with _patch_db(setting=None):
            with pytest.raises(HTTPException) as exc:
                await orch_main._check_vm_permission(None, job_needs_vm=True)
            assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_absent_setting_is_fail_open_enabled(self):
        """No vm_workspaces row = kill-switch not engaged, proceed to per-user."""
        user = {"id": "u6", "is_admin": False, "can_use_vm": True}
        with _patch_db(setting=None):
            await orch_main._check_vm_permission(user, job_needs_vm=True)

    @pytest.mark.asyncio
    async def test_malformed_setting_is_fail_open(self):
        """Non-dict value = falls through to per-user check, no crash."""
        admin = {"id": "u7", "is_admin": True, "can_use_vm": False}
        with _patch_db(setting={"value": "garbage"}):
            await orch_main._check_vm_permission(admin, job_needs_vm=True)

    @pytest.mark.asyncio
    async def test_enabled_true_is_noop_path(self):
        """Explicit enabled:true should defer to per-user check."""
        user = {"id": "u8", "is_admin": False, "can_use_vm": False}
        with _patch_db(setting={"value": {"enabled": True}}):
            with pytest.raises(HTTPException) as exc:
                await orch_main._check_vm_permission(user, job_needs_vm=True)
            assert exc.value.status_code == 403
            assert "not permitted" in exc.value.detail

    @pytest.mark.asyncio
    async def test_db_read_error_is_non_fatal(self):
        """A DB read failure on the kill-switch shouldn't hard-fail the gate."""
        user = {"id": "u9", "is_admin": False, "can_use_vm": True}
        with _patch_db(setting_error=RuntimeError("db down")):
            await orch_main._check_vm_permission(user, job_needs_vm=True)


# ---------------------------------------------------------------------------
# _job_needs_vm — the feeder for the gate
# ---------------------------------------------------------------------------


class TestJobNeedsVm:
    def test_config_override_vm_triggers(self):
        job = {"config_override": {"workspace": {"backend": "vm"}}}
        assert orch_main._job_needs_vm(job) is True

    def test_config_override_legacy_remote_triggers(self):
        job = {"config_override": {"workspace": {"backend": "remote"}}}
        assert orch_main._job_needs_vm(job) is True

    def test_config_override_sandbox_does_not_trigger(self):
        job = {"config_override": {"workspace": {"backend": "sandbox"}}}
        assert orch_main._job_needs_vm(job) is False

    def test_context_vm_requested_triggers(self):
        job = {"context": {"vm": {"requested": True}}}
        assert orch_main._job_needs_vm(job) is True

    def test_empty_job_does_not_trigger(self):
        assert orch_main._job_needs_vm({}) is False
