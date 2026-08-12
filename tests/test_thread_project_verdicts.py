"""Per-item project attachment verdicts (config-drift reporting layer)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.main import ProjectVerdict, _classify_thread_project_ids


USER = {"id": "11111111-1111-4111-8111-111111111111", "is_admin": False}
PROJECT_ALIVE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
PROJECT_GONE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PROJECT_NO_ROLE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


@pytest.mark.asyncio
async def test_classify_projects_reports_deleted_and_revoked():
    projects = {
        PROJECT_ALIVE: {"id": PROJECT_ALIVE},
        PROJECT_NO_ROLE: {"id": PROJECT_NO_ROLE},
    }
    roles = {PROJECT_ALIVE: "owner"}

    with patch("orchestrator.main.postgres_db") as db:
        db.get_project = AsyncMock(side_effect=lambda pid: projects.get(pid))
        db.get_user_role_in_project = AsyncMock(
            side_effect=lambda pid, uid: roles.get(pid)
        )

        verdicts = await _classify_thread_project_ids(
            USER, [PROJECT_ALIVE, PROJECT_GONE, PROJECT_NO_ROLE]
        )

    assert verdicts == [
        ProjectVerdict(PROJECT_ALIVE, False, None),
        ProjectVerdict(PROJECT_GONE, True, "deleted"),
        ProjectVerdict(PROJECT_NO_ROLE, True, "revoked"),
    ]


@pytest.mark.asyncio
async def test_admin_is_allowed_on_any_existing_project():
    with patch("orchestrator.main.postgres_db") as db:
        db.get_project = AsyncMock(return_value={"id": PROJECT_ALIVE})
        db.get_user_role_in_project = AsyncMock(return_value=None)

        verdicts = await _classify_thread_project_ids(
            {"id": USER["id"], "is_admin": True}, [PROJECT_ALIVE]
        )

    assert verdicts == [ProjectVerdict(PROJECT_ALIVE, False, None)]


@pytest.mark.asyncio
async def test_admin_still_denied_on_deleted_project():
    with patch("orchestrator.main.postgres_db") as db:
        db.get_project = AsyncMock(return_value=None)
        db.get_user_role_in_project = AsyncMock(return_value=None)

        verdicts = await _classify_thread_project_ids(
            {"id": USER["id"], "is_admin": True}, [PROJECT_GONE]
        )

    assert verdicts == [ProjectVerdict(PROJECT_GONE, True, "deleted")]
