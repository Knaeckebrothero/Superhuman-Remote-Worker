"""Contract tests for the orchestrator capability-grants glue (Slice 2): the
save-time and dispatch PEP helpers in orchestrator/main.py, exercised against a
mocked postgres_db. The pure PDP is covered in test_capability_grants.py.

Route-level ``duplicate_expert`` tests live here too — a deliberate exception
to this file's usual grain, because the routing/wiring details they pin
(whether the save-time gate runs at all, and now which *policy* it runs) can't
be observed from a bare helper call. They are not in
tests/test_tool_override_boundary.py::TestExpertWriteBoundary alongside the
kill-switch half of the same fix: that module's docstring is explicit that its
validator is "NOT an authorization gate" and that capability grants are a
separate PDP pinned elsewhere, and its ``expert_env`` fixture stubs
``_enforce_expert_save`` out specifically so those tests see the shape gate in
isolation. This file, not that one, is the PDP's home.

2026-08-04 (docs/superpowers/plans/2026-08-04-expert-write-gate-holes.md, task
3): ``duplicate_expert``'s grants half changed from refuse-outright to
strip-and-report — measured against the real PDP with default grants, refusing
blocked 7 of the 11 shipped experts, including ``scholar``, the route's own
advertised use ("start from scholar"). The other four expert-write routes are
unchanged and still refuse; ``_enforce_save_grants``'s own tests below assert
that.
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
async def test_duplicate_expert_strips_an_ungranted_tool_and_reports_it(monkeypatch):
    """The grants half of the fifth expert-write route
    (docs/issues/duplicate_expert_bypasses_user_experts_kill_switch.md), now
    strip-and-report rather than refuse (task 3 of the 2026-08-04 plan above):
    `duplicate` is the one route that forks a config someone else authored —
    visibility, not ownership, is the read check, so the source row's
    `tools.shell` grant requirement is never the copier's own to have
    satisfied, and refusing blocked the fork instead of just the shell tool.

    This was `test_duplicate_expert_denies_a_grant_the_source_config_requires`
    before task 3 — same source row, same missing grant, opposite outcome by
    design: 200 now, with the offending grant named in `dropped` and gone
    from the stored config, instead of a 422 with nothing stored.
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
        "config": {"tools": {"shell": ["run_command"], "git": ["git_status"]}},
    }
    fake = AsyncMock()
    fake.get_expert_visible_by_id = AsyncMock(return_value=source_row)
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": [], "project": [], "global": []}
    )
    fake.create_expert = AsyncMock(return_value={"id": "forked-id"})
    monkeypatch.setattr(m, "postgres_db", fake)

    non_admin_user = {"id": _UID, "is_admin": False}
    monkeypatch.setattr(
        m, "require_approved_user", AsyncMock(return_value=non_admin_user)
    )
    monkeypatch.setattr(m, "user_visible_project_ids", AsyncMock(return_value=[]))
    # Kill switch ON: this test isolates the grants half, not the switch.
    monkeypatch.setattr(m, "_user_experts_enabled", AsyncMock(return_value=True))

    result = await m.duplicate_expert(MagicMock(), str(uuid4()))

    assert result == {"id": "forked-id", "dropped": ["shell_tools"]}
    fake.create_expert.assert_awaited_once()
    assert fake.create_expert.await_args.kwargs["config"] == {
        "tools": {"git": ["git_status"]}
    }
    # Non-admin path proof (the free-admin-bypass trap this task's plan calls
    # out, tool_configuration_deferred_findings §4.1): is_admin is explicit
    # False above, and the grants resolver returns immediately for an admin
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

    `dropped` must be present and empty: nothing in this config is
    grant-gated, so the strip-and-report path (task 3) has nothing to strip.
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

    assert result == {**forked_row, "dropped": []}
    fake.create_expert.assert_awaited_once()
    assert fake.create_expert.await_args.kwargs["config"] == {
        "llm": {"model": "gemma-4-moe"}
    }


def _duplicate_env(monkeypatch, *, source_row, grant_rows=(), is_admin=False):
    """Shared scaffold for the strip-and-report tests below: a visible source
    row, a grants lookup returning `grant_rows` for the user scope, and the
    kill switch held open (each of these tests isolates the grants half, same
    as the two above)."""
    fake = AsyncMock()
    fake.get_expert_visible_by_id = AsyncMock(return_value=source_row)
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": list(grant_rows), "project": [], "global": []}
    )
    fake.create_expert = AsyncMock(side_effect=lambda **kw: {"id": "forked-id", **kw})
    monkeypatch.setattr(m, "postgres_db", fake)
    monkeypatch.setattr(
        m,
        "require_approved_user",
        AsyncMock(return_value={"id": _UID, "is_admin": is_admin}),
    )
    monkeypatch.setattr(m, "user_visible_project_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(m, "_user_experts_enabled", AsyncMock(return_value=True))
    return fake


@pytest.mark.asyncio
async def test_duplicate_expert_holder_of_the_grant_gets_an_unmodified_copy(
    monkeypatch,
):
    """Test group 5 of task 3: stripping must not fire for a copier who
    already holds what the source needs — otherwise it could be firing for
    everyone and test_duplicate_expert_strips_an_ungranted_tool_and_reports_it
    above would not be able to tell the difference."""
    config = {"tools": {"shell": ["run_command"], "git": ["git_status"]}}
    fake = _duplicate_env(
        monkeypatch,
        source_row={
            "id": str(uuid4()),
            "owner_id": str(uuid4()),
            "expert_type": "session",
            "name": "shared",
            "display_name": "Shared",
            "icon": "smart_toy",
            "color": "#6B7280",
            "config": config,
        },
        grant_rows=[{"key": "shell_tools", "value_json": True}],
    )

    result = await m.duplicate_expert(MagicMock(), str(uuid4()))

    assert result["dropped"] == []
    assert fake.create_expert.await_args.kwargs["config"] == config


@pytest.mark.asyncio
async def test_duplicate_expert_admin_copier_bypasses_and_reports_nothing_dropped(
    monkeypatch,
):
    """Admins bypass grants entirely (same as `_enforce_save_grants`) — the
    strip-and-report path must short-circuit the same way, not merely happen
    to strip nothing because an admin's resolved grants are permissive."""
    config = {"tools": {"shell": ["run_command"]}}
    fake = _duplicate_env(
        monkeypatch,
        source_row={
            "id": str(uuid4()),
            "owner_id": str(uuid4()),
            "expert_type": "session",
            "name": "shared",
            "display_name": "Shared",
            "icon": "smart_toy",
            "color": "#6B7280",
            "config": config,
        },
        is_admin=True,
    )

    result = await m.duplicate_expert(MagicMock(), str(uuid4()))

    assert result["dropped"] == []
    assert fake.create_expert.await_args.kwargs["config"] == config
    fake.list_grants_for_scopes.assert_not_awaited()  # admin bypass, no DB grants lookup


@pytest.mark.asyncio
async def test_duplicate_expert_safety_recheck_fires_when_the_strip_map_misses(
    monkeypatch,
):
    """Test group 3 of task 3 — THE SAFETY PROPERTY. `strip_to_grants` is
    forced to a no-op (as if a rule were missing from its map); the route
    must still 422 rather than create a row, because `_strip_save_grants`
    re-runs `evaluate` on whatever comes back and refuses on any residual
    violation. This is what makes an incomplete strip map merely a false
    refusal, never a permitted escape.
    """
    import src.core.capability_grants as capability_grants

    monkeypatch.setattr(
        capability_grants,
        "strip_to_grants",
        lambda fragment, grants: (fragment, []),  # claims nothing to drop
    )
    fake = _duplicate_env(
        monkeypatch,
        source_row={
            "id": str(uuid4()),
            "owner_id": str(uuid4()),
            "expert_type": "session",
            "name": "shared",
            "display_name": "Shared",
            "icon": "smart_toy",
            "color": "#6B7280",
            "config": {"tools": {"shell": ["run_command"]}},
        },
    )

    with pytest.raises(HTTPException) as ei:
        await m.duplicate_expert(MagicMock(), str(uuid4()))

    assert ei.value.status_code == 422
    assert "shell_tools" in ei.value.detail
    fake.create_expert.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_expert_scholar_end_to_end_for_a_default_grants_user(
    monkeypatch,
):
    """Test group 4 of task 3, and the one that proves the reported defect is
    actually fixed: `scholar` is the expert the route's own docstring names
    ("start from scholar"), a bundled (disk) expert, not a DB row — so this
    goes through `_bundled_expert_bundle`, the real config.yaml, unlike every
    other test in this file. A default-grants non-admin gets a 200, the
    stored row has no `tools.shell` and no `tools.delegation`, and the
    response names both as dropped.
    """
    fake = AsyncMock()
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": [], "project": [], "global": []}
    )
    fake.create_expert = AsyncMock(side_effect=lambda **kw: {"id": "forked-id", **kw})
    monkeypatch.setattr(m, "postgres_db", fake)
    monkeypatch.setattr(
        m,
        "require_approved_user",
        AsyncMock(return_value={"id": _UID, "is_admin": False}),
    )
    monkeypatch.setattr(m, "_user_experts_enabled", AsyncMock(return_value=True))
    # A bundled (disk) expert_id skips the visibility lookup entirely (it's
    # only for DB rows) — this is the copier's OWN project scope, resolved
    # while computing THEIR grants (_grant_project_ids), and is exercised
    # regardless of the source expert's origin. Explicit here (rather than
    # relying on AsyncMock's default empty-iterable return) so the "no
    # project-level grant" premise is visible, not incidental.
    monkeypatch.setattr(m, "user_visible_project_ids", AsyncMock(return_value=[]))

    result = await m.duplicate_expert(MagicMock(), "scholar")

    assert set(result["dropped"]) == {"shell_tools", "delegation"}
    stored = fake.create_expert.await_args.kwargs["config"]
    assert "shell" not in stored.get("tools", {})
    assert "delegation" not in stored.get("tools", {})
    assert "enabled" not in stored.get("delegation", {})
    # Scholar's delegation.mode is not the violation (only .enabled is) and
    # must survive — proof the strip did not take the whole settings dict.
    assert stored["delegation"]["mode"] == "light"


# The brief's own measurement: default grants (shell_tools=False,
# delegation=False) refuse 7 of the 11 shipped experts before task 3 —
# scholar, developer, critic (shell + delegation) and bughunter, designer,
# designer-interactive, product-qa (shell). This is the after: every one
# forks (200) for the same default-grants non-admin, and drops exactly the
# grants the brief measured, no more and no less.
_PREVIOUSLY_REFUSED_SHIPPED_EXPERTS = {
    "scholar": {"shell_tools", "delegation"},
    "developer": {"shell_tools", "delegation"},
    "critic": {"shell_tools", "delegation"},
    "bughunter": {"shell_tools"},
    "designer": {"shell_tools"},
    "designer-interactive": {"shell_tools"},
    "product-qa": {"shell_tools"},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("expert_id", sorted(_PREVIOUSLY_REFUSED_SHIPPED_EXPERTS))
async def test_previously_refused_shipped_experts_now_fork_for_default_grants(
    monkeypatch, expert_id
):
    """Measured before/after for every one of the 7 shipped experts the brief
    names as blocked by Task 1's refuse-outright grants check. Before: 422,
    nothing stored (measured directly against this repo's real bundled
    configs while writing this fix — see the task report). After: 200,
    dropped is exactly the grant set named in the brief, never more (an
    over-broad strip would silently degrade an expert further than its own
    config warrants) and never less (that would be the escape the safety
    re-check exists to close).
    """
    fake = AsyncMock()
    fake.list_grants_for_scopes = AsyncMock(
        return_value={"user": [], "project": [], "global": []}
    )
    fake.create_expert = AsyncMock(side_effect=lambda **kw: {"id": "forked-id", **kw})
    monkeypatch.setattr(m, "postgres_db", fake)
    monkeypatch.setattr(
        m,
        "require_approved_user",
        AsyncMock(return_value={"id": _UID, "is_admin": False}),
    )
    monkeypatch.setattr(m, "_user_experts_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(m, "user_visible_project_ids", AsyncMock(return_value=[]))

    result = await m.duplicate_expert(MagicMock(), expert_id)

    assert set(result["dropped"]) == _PREVIOUSLY_REFUSED_SHIPPED_EXPERTS[expert_id]
    stored = fake.create_expert.await_args.kwargs["config"]
    # The stored config must actually be clean against the same default
    # grants, not merely have a `dropped` list that claims so.
    from src.core.capability_grants import CATALOG, evaluate

    default_grants = {k: v["default"] for k, v in CATALOG.items()}
    assert evaluate(stored, default_grants) == []
