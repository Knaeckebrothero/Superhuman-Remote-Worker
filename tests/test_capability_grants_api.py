"""Contract tests for the orchestrator capability-grants glue (Slice 2): the
save-time and dispatch PEP helpers in orchestrator/main.py, exercised against a
mocked postgres_db. The pure PDP is covered in test_capability_grants.py.

One route-level test lives here too
(``test_duplicate_expert_denies_a_grant_the_source_config_requires``): the
grants half of docs/issues/duplicate_expert_bypasses_user_experts_kill_switch.md.
It calls the actual ``duplicate_expert`` endpoint rather than a bare helper —
a deliberate exception to this file's usual grain — because the defect it
pins is a *wiring* gap (the route never called the save-time PEP at all), which
a helper-level call cannot observe. It is not in
tests/test_tool_override_boundary.py::TestExpertWriteBoundary alongside the
kill-switch half of the same fix: that module's docstring is explicit that its
validator is "NOT an authorization gate" and that capability grants are a
separate PDP pinned elsewhere, and its ``expert_env`` fixture stubs
``_enforce_expert_save`` out specifically so those tests see the shape gate in
isolation. This file, not that one, is the PDP's home.
"""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

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


@pytest.mark.asyncio
async def test_enforce_job_create_grants_raises_422_for_ungranted(monkeypatch):
    # Job submit-time PEP: an ungranted override is refused at create. Without
    # it the job is accepted and only denied at dispatch, which marks it failed
    # — so a session agent that created it has already reported success.
    fake = AsyncMock()
    fake.get_user = AsyncMock(return_value={"id": _UID, "is_admin": False})
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": [], "project": [], "global": []}
    )
    monkeypatch.setattr(m, "postgres_db", fake)
    with pytest.raises(HTTPException) as ei:
        await m._enforce_job_create_grants(
            {"workspace": {"backend": "vm"}}, user_id=_UID, project_ids=[]
        )
    assert ei.value.status_code == 422
    assert "vm_workspace" in str(ei.value.detail)


@pytest.mark.asyncio
async def test_enforce_job_create_grants_allows_granted_override(monkeypatch):
    _patch_grants_db(
        monkeypatch,
        user_rows=[{"key": "vm_workspace", "value_json": True}],
    )
    await m._enforce_job_create_grants(
        {"workspace": {"backend": "vm"}}, user_id=_UID, project_ids=[]
    )


@pytest.mark.asyncio
async def test_enforce_job_create_grants_admin_bypass(monkeypatch):
    fake = AsyncMock()
    fake.get_user = AsyncMock(return_value={"id": _UID, "is_admin": True})
    monkeypatch.setattr(m, "postgres_db", fake)
    await m._enforce_job_create_grants(
        {"autonomy": "full"}, user_id=_UID, project_ids=[]
    )


@pytest.mark.asyncio
async def test_enforce_job_create_grants_skips_userless_and_empty(monkeypatch):
    """Userless system children have no principal whose grants to resolve, and
    an empty override has nothing to check — neither may touch the DB."""
    fake = AsyncMock()
    fake.get_user = AsyncMock(side_effect=AssertionError("must not hit the DB"))
    monkeypatch.setattr(m, "postgres_db", fake)
    await m._enforce_job_create_grants(
        {"workspace": {"backend": "vm"}}, user_id=None, project_ids=[]
    )
    await m._enforce_job_create_grants(None, user_id=_UID, project_ids=[])
    await m._enforce_job_create_grants({}, user_id=_UID, project_ids=[])


@pytest.mark.asyncio
async def test_duplicate_expert_denies_a_grant_the_source_config_requires(monkeypatch):
    """The grants half of the fifth expert-write route
    (docs/issues/duplicate_expert_bypasses_user_experts_kill_switch.md). The
    kill-switch half is pinned, parametrised over all five write routes, in
    tests/test_tool_override_boundary.py::TestExpertWriteBoundary
    (``test_kill_switch_403s_every_write_route``); this is `duplicate`
    specifically, because it is the one route that forks a config someone
    else authored — visibility, not ownership, is the read check, so the
    source row's `tools.shell` grant requirement is never the copier's own to
    have satisfied.

    Both assertions matter: a 422 alone would pass if `_create_forked_expert`
    ran first and the row merely got created anyway.
    """
    source_row = {
        "id": str(uuid4()),
        "owner_id": str(uuid4()),  # someone else's row; visible, not owned
        "expert_type": "session",
        "name": "shared",
        "display_name": "Shared",
        "icon": "smart_toy",
        "color": "#6B7280",
        # A real, correctly-categorised (shape-valid) shell tool list — not a
        # smuggle — that the copier's own grants do not cover: shell_tools
        # defaults to deny in the CATALOG.
        "config": {"tools": {"shell": ["run_command"]}},
    }
    fake = AsyncMock()
    fake.get_expert_visible_by_id = AsyncMock(return_value=source_row)
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": [], "project": [], "global": []}
    )
    monkeypatch.setattr(m, "postgres_db", fake)

    non_admin_user = {"id": _UID, "is_admin": False}
    monkeypatch.setattr(
        m, "require_approved_user", AsyncMock(return_value=non_admin_user)
    )
    monkeypatch.setattr(m, "user_visible_project_ids", AsyncMock(return_value=[]))
    # Kill switch ON: this test isolates the grants half, not the switch.
    monkeypatch.setattr(m, "_user_experts_enabled", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as ei:
        await m.duplicate_expert(MagicMock(), str(uuid4()))

    assert ei.value.status_code == 422
    assert "shell_tools" in ei.value.detail
    fake.create_expert.assert_not_awaited()
    # Non-admin path proof (the free-admin-bypass trap this task's plan calls
    # out, tool_configuration_deferred_findings §4.1): is_admin is explicit
    # False above, and _enforce_save_grants returns immediately for an admin
    # WITHOUT touching the DB — so an awaited grants lookup is direct evidence
    # this ran the non-admin branch rather than a Mock truthy-bypass quietly
    # making the assertions above vacuous.
    assert non_admin_user["is_admin"] is False
    fake.list_grants_for_scopes.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_expert_still_forks_when_the_gate_allows(monkeypatch):
    """The new call is a gate, not a detour. Neither denial test above can
    show the added line leaves the working case alone — one exercises the
    switch, the other the grants 422 — so this pins the third outcome: a
    copier who clears the kill switch and holds every grant the source needs
    still gets the forked row back. Also guards the specific instruction to
    pass `src["config"]` (not `src.get("config") or {}`): a typo'd name here
    would raise before `_create_forked_expert` ever ran.
    """
    source_row = {
        "id": str(uuid4()),
        "owner_id": str(uuid4()),  # visible, still not the copier's own row
        "expert_type": "session",
        "name": "shared",
        "display_name": "Shared",
        "icon": "smart_toy",
        "color": "#6B7280",
        "config": {"llm": {"model": "gemma-4-moe"}},  # nothing grant-gated
    }
    forked_row = {"id": str(uuid4()), "name": "shared-copy"}
    fake = AsyncMock()
    fake.get_expert_visible_by_id = AsyncMock(return_value=source_row)
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": [], "project": [], "global": []}
    )
    fake.create_expert = AsyncMock(return_value=forked_row)
    monkeypatch.setattr(m, "postgres_db", fake)
    monkeypatch.setattr(
        m,
        "require_approved_user",
        AsyncMock(return_value={"id": _UID, "is_admin": False}),
    )
    monkeypatch.setattr(m, "user_visible_project_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(m, "_user_experts_enabled", AsyncMock(return_value=True))

    result = await m.duplicate_expert(MagicMock(), str(uuid4()))

    assert result == forked_row
    fake.create_expert.assert_awaited_once()
