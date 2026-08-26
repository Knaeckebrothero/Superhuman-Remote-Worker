"""The officer roster — GET /api/officers (officer_legate_channel.md).

Discovery, not a dashboard: with dozens of projects, "which of these has an
officer and what is he doing" must be one call. Two invariants have teeth:

* It reads ``project_officers`` and never creates a post. The per-project card
  endpoint is ``get_or_create``; fanning that across every project would
  commission-by-side-effect.
* It is scoped like every other list — a project the caller cannot see is not
  on the roster, and an MCP token narrowed to one project sees one row.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import main as orch_main
from main import list_officers

PROJECT_A = str(uuid4())
PROJECT_B = str(uuid4())
THREAD_A = str(uuid4())
TODAY = datetime.now(timezone.utc).date().isoformat()


def _row(**over) -> dict:
    row = {
        "project_id": PROJECT_A,
        "project_name": "Better Resavio",
        "thread_id": THREAD_A,
        "thread_project_id": PROJECT_A,
        "thread_status": "active",
        "post_config_override": {"officer": {"auto_pull": False}},
        "metadata": {
            "config_override": {
                "llm": {"model": "gpt-5.6-sol"},
                "officer": {"enabled": True, "auto_pull": False},
            },
            "officer_state": {
                "pages": {"date": TODAY, "count": 2},
                "digest": [{"subject": "s"}, {"subject": "t"}],
            },
        },
        "next_wake_at": datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc),
        "pending_events": 3,
        "in_flight_jobs": 1,
        "last_agent_activity": datetime(2026, 8, 17, 6, 11, tzinfo=timezone.utc),
    }
    row.update(over)
    return row


@pytest.fixture
def db(monkeypatch):
    db = SimpleNamespace(
        list_project_officer_posts=AsyncMock(return_value=[_row()]),
        get_or_create_project_officer=AsyncMock(),
    )
    monkeypatch.setattr(orch_main, "postgres_db", db)
    return db


@pytest.fixture
def as_user(monkeypatch):
    monkeypatch.setattr(
        orch_main, "require_approved_user", AsyncMock(return_value={"id": "u1"})
    )


@pytest.mark.asyncio
async def test_the_roster_reports_the_post_at_a_glance(db, as_user, monkeypatch):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value={PROJECT_A})
    )

    result = await list_officers(MagicMock())

    officer = result["officers"][0]
    assert officer["project_name"] == "Better Resavio"
    assert officer["commissioned"] is True
    assert officer["held"] is None
    assert officer["next_wake_at"] == "2026-08-17T09:30:00+00:00"
    assert officer["pending_events"] == 3
    assert officer["in_flight_jobs"] == 1
    assert officer["pages_today"] == 2
    assert officer["digest_waiting"] == 2
    assert officer["model"] == "gpt-5.6-sol"
    assert officer["auto_pull_durable"] is False
    assert officer["auto_pull_runtime"] is False
    assert officer["auto_pull_mirror_consistent"] is True


@pytest.mark.asyncio
async def test_a_vacant_post_is_listed_as_vacant(db, as_user, monkeypatch):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value={PROJECT_A})
    )
    db.list_project_officer_posts = AsyncMock(
        return_value=[_row(thread_id=None, thread_status=None, metadata=None)]
    )

    officer = (await list_officers(MagicMock()))["officers"][0]

    assert officer["commissioned"] is False
    assert officer["thread_id"] is None


@pytest.mark.asyncio
async def test_a_held_officer_says_so(db, as_user, monkeypatch):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value={PROJECT_A})
    )
    metadata = _row()["metadata"]
    metadata["config_override"]["officer"]["hold"] = {"kind": "conference"}
    db.list_project_officer_posts = AsyncMock(return_value=[_row(metadata=metadata)])

    officer = (await list_officers(MagicMock()))["officers"][0]

    assert officer["held"] == {"kind": "conference"}


@pytest.mark.asyncio
async def test_the_roster_is_scoped_to_visible_projects(db, as_user, monkeypatch):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value={PROJECT_B})
    )

    await list_officers(MagicMock())

    assert db.list_project_officer_posts.await_args.args[0] == [PROJECT_B]


@pytest.mark.asyncio
async def test_an_admin_sees_every_post_without_materializing_ids(
    db, as_user, monkeypatch
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value="all")
    )

    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", False)
    result = await list_officers(MagicMock())

    assert db.list_project_officer_posts.await_args.args[0] is None
    assert result["auto_pull_downgrade"] == {
        "scope": "all_projects",
        "safe": True,
        "release_fence_closed": True,
        "durable_enabled": 0,
        "runtime_enabled": 0,
        "invalid_values": 0,
        "mirror_mismatches": 0,
        "reason": None,
    }


@pytest.mark.asyncio
async def test_admin_downgrade_readiness_requires_the_release_fence_closed(
    db, as_user, monkeypatch
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value="all")
    )
    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", True)

    readiness = (await list_officers(MagicMock()))["auto_pull_downgrade"]

    assert readiness["safe"] is False
    assert readiness["release_fence_closed"] is False
    assert readiness["reason"] == "release_fence_open"


@pytest.mark.asyncio
async def test_visible_project_readiness_is_never_global_downgrade_proof(
    db, as_user, monkeypatch
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value={PROJECT_A})
    )
    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", False)

    readiness = (await list_officers(MagicMock()))["auto_pull_downgrade"]

    assert readiness["scope"] == "visible_projects"
    assert readiness["safe"] is False
    assert readiness["reason"] == "all_projects_admin_scope_required"


@pytest.mark.asyncio
async def test_malformed_commissioned_mirror_fails_downgrade_readiness(
    db, as_user, monkeypatch
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value="all")
    )
    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", False)
    db.list_project_officer_posts = AsyncMock(return_value=[_row(metadata=None)])

    result = await list_officers(MagicMock())

    assert result["officers"][0]["auto_pull_runtime_valid"] is False
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["invalid_values"] == 1
    assert result["auto_pull_downgrade"]["mirror_mismatches"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [[], "[]", '"legacy-scalar"', 7])
async def test_non_object_legacy_metadata_is_invalid_not_a_roster_500(
    db, as_user, monkeypatch, metadata
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value="all")
    )
    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", False)
    db.list_project_officer_posts = AsyncMock(return_value=[_row(metadata=metadata)])

    result = await list_officers(MagicMock())

    officer = result["officers"][0]
    assert officer["auto_pull_runtime_valid"] is False
    assert officer["model"] is None
    assert officer["pages_today"] == 0
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["invalid_values"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("post_config", [[], 0, False, "", None])
async def test_falsey_malformed_durable_post_blocks_downgrade_readiness(
    db, as_user, monkeypatch, post_config
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value="all")
    )
    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", False)
    db.list_project_officer_posts = AsyncMock(
        return_value=[
            _row(
                thread_id=None,
                thread_status=None,
                thread_project_id=None,
                metadata=None,
                post_config_override=post_config,
            )
        ]
    )

    result = await list_officers(MagicMock())

    officer = result["officers"][0]
    assert officer["auto_pull_durable_valid"] is False
    assert officer["auto_pull_mirror_consistent"] is False
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["invalid_values"] == 1
    assert result["auto_pull_downgrade"]["mirror_mismatches"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("officer_config", [[], 0, False, "", None])
async def test_falsey_malformed_durable_officer_blocks_downgrade_readiness(
    db, as_user, monkeypatch, officer_config
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value="all")
    )
    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", False)
    db.list_project_officer_posts = AsyncMock(
        return_value=[
            _row(
                thread_id=None,
                thread_status=None,
                thread_project_id=None,
                metadata=None,
                post_config_override={"officer": officer_config},
            )
        ]
    )

    result = await list_officers(MagicMock())

    officer = result["officers"][0]
    assert officer["auto_pull_durable_valid"] is False
    assert officer["auto_pull_mirror_consistent"] is False
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["invalid_values"] == 1
    assert result["auto_pull_downgrade"]["mirror_mismatches"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("post_config", [{}, {"officer": {}}])
async def test_absent_durable_officer_is_a_safe_false_default(
    db, as_user, monkeypatch, post_config
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value="all")
    )
    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", False)
    db.list_project_officer_posts = AsyncMock(
        return_value=[
            _row(
                thread_id=None,
                thread_status=None,
                thread_project_id=None,
                metadata=None,
                post_config_override=post_config,
            )
        ]
    )

    result = await list_officers(MagicMock())

    officer = result["officers"][0]
    assert officer["auto_pull_durable"] is False
    assert officer["auto_pull_durable_valid"] is True
    assert officer["auto_pull_mirror_consistent"] is True
    assert result["auto_pull_downgrade"]["safe"] is True


@pytest.mark.asyncio
async def test_malformed_auxiliary_submaps_do_not_obscure_valid_authority(
    db, as_user, monkeypatch
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value="all")
    )
    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", False)
    db.list_project_officer_posts = AsyncMock(
        return_value=[
            _row(
                metadata={
                    "config_override": {
                        "officer": {"enabled": True, "auto_pull": False},
                        "llm": ["legacy"],
                    },
                    "officer_state": ["legacy"],
                }
            )
        ]
    )

    result = await list_officers(MagicMock())

    officer = result["officers"][0]
    assert officer["auto_pull_runtime_valid"] is True
    assert officer["model"] is None
    assert officer["pages_today"] == 0
    assert result["auto_pull_downgrade"]["safe"] is True


@pytest.mark.asyncio
async def test_ended_but_linked_true_mirror_blocks_downgrade(db, as_user, monkeypatch):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value="all")
    )
    monkeypatch.setattr(orch_main, "OFFICER_AUTO_PULL_RELEASE_ENABLED", False)
    metadata = _row()["metadata"]
    metadata["config_override"]["officer"]["auto_pull"] = True
    db.list_project_officer_posts = AsyncMock(
        return_value=[
            _row(
                thread_status="ended",
                metadata=metadata,
                post_config_override={"officer": {"auto_pull": False}},
            )
        ]
    )

    result = await list_officers(MagicMock())

    assert result["officers"][0]["commissioned"] is False
    assert result["officers"][0]["auto_pull_runtime"] is True
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["runtime_enabled"] == 1
    assert result["auto_pull_downgrade"]["mirror_mismatches"] == 1


@pytest.mark.asyncio
async def test_a_user_with_no_visible_projects_gets_an_empty_roster(
    db, as_user, monkeypatch
):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value=set())
    )

    assert (await list_officers(MagicMock()))["officers"] == []
    db.list_project_officer_posts.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_roster_never_creates_a_post(db, as_user, monkeypatch):
    monkeypatch.setattr(
        orch_main, "user_visible_project_ids", AsyncMock(return_value={PROJECT_A})
    )

    await list_officers(MagicMock())

    db.get_or_create_project_officer.assert_not_awaited()
