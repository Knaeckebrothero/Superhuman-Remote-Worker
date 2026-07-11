"""Contract tests for the orchestrator capability-grants glue (Slice 2): the
save-time and dispatch PEP helpers in orchestrator/main.py, exercised against a
mocked postgres_db. The pure PDP is covered in test_capability_grants.py.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import orchestrator.main as m

_UID = "11111111-1111-1111-1111-111111111111"


def _patch_grants_db(monkeypatch, *, user_rows=None, is_admin=False):
    fake = AsyncMock()
    fake.get_user = AsyncMock(return_value={"id": _UID, "is_admin": is_admin})
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": user_rows or [], "project": [], "global": []}
    )
    monkeypatch.setattr(m, "postgres_db", fake)
    return fake


def test_violations_detail_lists_keys():
    assert "shell_tools" in m._grant_violations_detail(
        ["shell_tools: tools.shell requires the shell_tools grant"]
    )


@pytest.mark.asyncio
async def test_enforce_save_grants_admin_bypasses():
    # Admin short-circuits before any DB call (no mock needed).
    await m._enforce_save_grants(
        {"tools": {"shell": ["ls"]}}, user={"id": _UID, "is_admin": True}
    )


@pytest.mark.asyncio
async def test_enforce_save_grants_raises_422_for_ungranted(monkeypatch):
    fake = AsyncMock()
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": [], "project": [], "global": []}
    )
    fake.get_projects_for_user = AsyncMock(return_value=[])
    monkeypatch.setattr(m, "postgres_db", fake)
    with pytest.raises(HTTPException) as ei:
        await m._enforce_save_grants(
            {"tools": {"shell": ["ls"]}}, user={"id": _UID, "is_admin": False}
        )
    assert ei.value.status_code == 422 and "shell_tools" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_enforce_dispatch_grants_raises_grantdenied(monkeypatch):
    _patch_grants_db(monkeypatch)
    with pytest.raises(m.GrantDenied) as ei:
        await m._enforce_dispatch_grants(
            {"tools": {"shell": ["ls"]}}, runner_user_id=_UID, project_ids=[]
        )
    assert "shell_tools" in str(ei.value)


@pytest.mark.asyncio
async def test_enforce_dispatch_grants_admin_bypass(monkeypatch):
    _patch_grants_db(monkeypatch, is_admin=True)
    # No violation despite ungranted shell — admin runner bypasses.
    await m._enforce_dispatch_grants(
        {"tools": {"shell": ["ls"]}}, runner_user_id=_UID, project_ids=[]
    )


@pytest.mark.asyncio
async def test_enforce_dispatch_grants_allows_when_granted(monkeypatch):
    _patch_grants_db(
        monkeypatch,
        user_rows=[
            {"key": "shell_tools", "value_json": True},
            {"key": "delegation", "value_json": True},
        ],
    )
    # Granted shell + delegation -> the worker-base-shaped fragment passes.
    await m._enforce_dispatch_grants(
        {"tools": {"shell": ["ls"], "delegation": ["delegate_work"]}},
        runner_user_id=_UID,
        project_ids=[],
    )


@pytest.mark.asyncio
async def test_lifecycle_runner_allows_full_autonomy(monkeypatch):
    _patch_grants_db(monkeypatch)
    await m._enforce_dispatch_grants(
        {"autonomy": "full"},
        runner_user_id=_UID,
        project_ids=[],
        runner_kind="lifecycle",
    )


@pytest.mark.asyncio
async def test_user_runner_still_denies_full_autonomy(monkeypatch):
    _patch_grants_db(monkeypatch)
    with pytest.raises(m.GrantDenied) as ei:
        await m._enforce_dispatch_grants(
            {"autonomy": "full"},
            runner_user_id=_UID,
            project_ids=[],
            runner_kind="user",
        )
    assert "autonomy_ceiling" in str(ei.value)


@pytest.mark.asyncio
async def test_lifecycle_runner_keeps_owner_capability_limits(monkeypatch):
    _patch_grants_db(monkeypatch)
    with pytest.raises(m.GrantDenied) as ei:
        await m._enforce_dispatch_grants(
            {"autonomy": "full", "workspace": {"backend": "vm"}},
            runner_user_id=_UID,
            project_ids=[],
            runner_kind="lifecycle",
        )
    assert "vm_workspace" in str(ei.value)
    assert "autonomy_ceiling" not in str(ei.value)


@pytest.mark.asyncio
async def test_enforce_session_create_grants_raises_422_for_denied_mode(monkeypatch):
    # Session create/update PEP (Layer 2): a permission_mode above the owner's
    # ceiling (default 'supervised', no grant) → 422 with the violation, so a
    # never-startable session is rejected at the API instead of timing out at
    # provisioning. docs/issues/session_permission_mode_grant_denied_ready_timeout.md
    fake = AsyncMock()
    fake.get_user = AsyncMock(return_value={"id": _UID, "is_admin": False})
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": [], "project": [], "global": []}
    )
    monkeypatch.setattr(m, "postgres_db", fake)
    with pytest.raises(HTTPException) as ei:
        await m._enforce_session_create_grants(
            {"interactive": {"permission_mode": "autonomous"}},
            user_id=_UID,
            project_ids=[],
        )
    assert ei.value.status_code == 422
    assert "permission_mode" in str(ei.value.detail)
    assert "autonomous" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_enforce_session_create_grants_admin_bypass(monkeypatch):
    fake = AsyncMock()
    fake.get_user = AsyncMock(return_value={"id": _UID, "is_admin": True})
    monkeypatch.setattr(m, "postgres_db", fake)
    # Admin owner bypasses — autonomous is fine.
    await m._enforce_session_create_grants(
        {"interactive": {"permission_mode": "autonomous"}},
        user_id=_UID,
        project_ids=[],
    )


@pytest.mark.asyncio
async def test_enforce_session_create_grants_allows_within_ceiling(monkeypatch):
    fake = AsyncMock()
    fake.get_user = AsyncMock(return_value={"id": _UID, "is_admin": False})
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": [], "project": [], "global": []}
    )
    monkeypatch.setattr(m, "postgres_db", fake)
    # 'supervised' is the default ceiling → within grants → no raise.
    await m._enforce_session_create_grants(
        {"interactive": {"permission_mode": "supervised"}},
        user_id=_UID,
        project_ids=[],
    )


@pytest.mark.asyncio
async def test_user_experts_kill_switch_default_enabled(monkeypatch):
    fake = AsyncMock()
    fake.get_system_setting = AsyncMock(return_value=None)  # absent row
    monkeypatch.setattr(m, "postgres_db", fake)
    assert await m._user_experts_enabled() is True


@pytest.mark.asyncio
async def test_user_experts_kill_switch_disabled(monkeypatch):
    fake = AsyncMock()
    fake.get_system_setting = AsyncMock(return_value={"value": {"enabled": False}})
    monkeypatch.setattr(m, "postgres_db", fake)
    assert await m._user_experts_enabled() is False
