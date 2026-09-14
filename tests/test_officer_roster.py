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
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.routers import officers as officers_router
from orchestrator.services import officer_post_views
from orchestrator.services.officer_post_views import (
    OfficerPostViewDependencies,
    list_officers as list_officers_service,
)

PROJECT_A = str(uuid4())
PROJECT_B = str(uuid4())
THREAD_A = str(uuid4())
USER = {"id": "u1"}


def _row(**over) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
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
                "pages": {"date": today, "count": 2},
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


def _deps(store, *, auto_pull_release_enabled=False) -> OfficerPostViewDependencies:
    """The roster's own collaborators. The release fence is a callable on the
    dependency object now, so a suite steers it here rather than by rebinding
    ``OFFICER_AUTO_PULL_RELEASE_ENABLED`` on ``orchestrator.main``."""
    return OfficerPostViewDependencies(
        store=store,
        vector_store=MagicMock(name="vector_db"),
        usage_ledger=MagicMock(name="usage_ledger"),
        persistent_provisioner=MagicMock(name="persistent_provisioner"),
        auto_pull_release_enabled=lambda: auto_pull_release_enabled,
        persistent_agent_reconciliation_enabled=lambda: False,
        find_open_conference_thread=AsyncMock(return_value=None),
    )


async def list_officers(request, store, *, auto_pull_release_enabled=False):
    """Drive the roster read through its owner with an explicit principal —
    the router resolves both from the application and the gate."""
    return await list_officers_service(
        request,
        dependencies=_deps(store, auto_pull_release_enabled=auto_pull_release_enabled),
        user=USER,
    )


@pytest.fixture
def db():
    return SimpleNamespace(
        list_project_officer_posts=AsyncMock(return_value=[_row()]),
        get_or_create_project_officer=AsyncMock(),
    )


@pytest.fixture
def visible(monkeypatch):
    """Point the scope read at the module that actually performs it."""

    def _set(value):
        monkeypatch.setattr(
            officer_post_views,
            "user_visible_project_ids",
            AsyncMock(return_value=value),
        )

    return _set


def _client(store, monkeypatch, *, auto_pull_release_enabled=False) -> TestClient:
    """The real route on a bare application, so the wiring stays covered: the
    router resolves its dependencies from ``app.state`` and runs the gate."""
    monkeypatch.setattr(
        officers_router, "require_approved_user", AsyncMock(return_value=USER)
    )
    app = FastAPI()
    app.state.officer_post_view_dependencies_factory = lambda: _deps(
        store, auto_pull_release_enabled=auto_pull_release_enabled
    )
    app.include_router(officers_router.router)
    return TestClient(app, raise_server_exceptions=False)


def test_the_roster_reports_the_post_at_a_glance(db, visible, monkeypatch):
    visible({PROJECT_A})

    result = _client(db, monkeypatch).get("/api/officers").json()

    officer = result["officers"][0]
    assert officer["project_name"] == "Better Resavio"
    assert officer["commissioned"] is True
    assert officer["held"] is None
    assert officer["next_wake_at"] == "2026-08-17T09:30:00+00:00"
    assert officer["pending_events"] == 3
    assert officer["in_flight_jobs"] == 1
    assert officer["model"] == "gpt-5.6-sol"
    assert officer["auto_pull_durable"] is False
    assert officer["auto_pull_runtime"] is False
    assert officer["auto_pull_mirror_consistent"] is True
    # Pages and digests are feed rows (unified notification system), read
    # from the notification center — the roster carries no counters for them.
    assert "pages_today" not in officer
    assert "digest_waiting" not in officer


@pytest.mark.asyncio
async def test_a_vacant_post_is_listed_as_vacant(db, visible):
    visible({PROJECT_A})
    db.list_project_officer_posts = AsyncMock(
        return_value=[_row(thread_id=None, thread_status=None, metadata=None)]
    )

    officer = (await list_officers(MagicMock(), db))["officers"][0]

    assert officer["commissioned"] is False
    assert officer["thread_id"] is None


@pytest.mark.asyncio
async def test_a_held_officer_says_so(db, visible):
    visible({PROJECT_A})
    metadata = _row()["metadata"]
    metadata["config_override"]["officer"]["hold"] = {"kind": "conference"}
    db.list_project_officer_posts = AsyncMock(return_value=[_row(metadata=metadata)])

    officer = (await list_officers(MagicMock(), db))["officers"][0]

    assert officer["held"] == {"kind": "conference"}


@pytest.mark.asyncio
async def test_the_roster_is_scoped_to_visible_projects(db, visible):
    visible({PROJECT_B})

    await list_officers(MagicMock(), db)

    assert db.list_project_officer_posts.await_args.args[0] == [PROJECT_B]


@pytest.mark.asyncio
async def test_an_admin_sees_every_post_without_materializing_ids(db, visible):
    visible("all")

    result = await list_officers(MagicMock(), db, auto_pull_release_enabled=False)

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
async def test_admin_downgrade_readiness_requires_the_release_fence_closed(db, visible):
    visible("all")

    readiness = (await list_officers(MagicMock(), db, auto_pull_release_enabled=True))[
        "auto_pull_downgrade"
    ]

    assert readiness["safe"] is False
    assert readiness["release_fence_closed"] is False
    assert readiness["reason"] == "release_fence_open"


@pytest.mark.asyncio
async def test_visible_project_readiness_is_never_global_downgrade_proof(db, visible):
    visible({PROJECT_A})

    readiness = (await list_officers(MagicMock(), db, auto_pull_release_enabled=False))[
        "auto_pull_downgrade"
    ]

    assert readiness["scope"] == "visible_projects"
    assert readiness["safe"] is False
    assert readiness["reason"] == "all_projects_admin_scope_required"


@pytest.mark.asyncio
async def test_malformed_commissioned_mirror_fails_downgrade_readiness(db, visible):
    visible("all")
    db.list_project_officer_posts = AsyncMock(return_value=[_row(metadata=None)])

    result = await list_officers(MagicMock(), db, auto_pull_release_enabled=False)

    assert result["officers"][0]["auto_pull_runtime_valid"] is False
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["invalid_values"] == 1
    assert result["auto_pull_downgrade"]["mirror_mismatches"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [[], "[]", '"legacy-scalar"', 7])
async def test_non_object_legacy_metadata_is_invalid_not_a_roster_500(
    db, visible, metadata
):
    visible("all")
    db.list_project_officer_posts = AsyncMock(return_value=[_row(metadata=metadata)])

    result = await list_officers(MagicMock(), db, auto_pull_release_enabled=False)

    officer = result["officers"][0]
    assert officer["auto_pull_runtime_valid"] is False
    assert officer["model"] is None
    assert "pages_today" not in officer
    assert "digest_waiting" not in officer
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["invalid_values"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("post_config", [[], 0, False, "", None])
async def test_falsey_malformed_durable_post_blocks_downgrade_readiness(
    db, visible, post_config
):
    visible("all")
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

    result = await list_officers(MagicMock(), db, auto_pull_release_enabled=False)

    officer = result["officers"][0]
    assert officer["auto_pull_durable_valid"] is False
    assert officer["auto_pull_mirror_consistent"] is False
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["invalid_values"] == 1
    assert result["auto_pull_downgrade"]["mirror_mismatches"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("officer_config", [[], 0, False, "", None])
async def test_falsey_malformed_durable_officer_blocks_downgrade_readiness(
    db, visible, officer_config
):
    visible("all")
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

    result = await list_officers(MagicMock(), db, auto_pull_release_enabled=False)

    officer = result["officers"][0]
    assert officer["auto_pull_durable_valid"] is False
    assert officer["auto_pull_mirror_consistent"] is False
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["invalid_values"] == 1
    assert result["auto_pull_downgrade"]["mirror_mismatches"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("post_config", [{}, {"officer": {}}])
async def test_absent_durable_officer_is_a_safe_false_default(db, visible, post_config):
    visible("all")
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

    result = await list_officers(MagicMock(), db, auto_pull_release_enabled=False)

    officer = result["officers"][0]
    assert officer["auto_pull_durable"] is False
    assert officer["auto_pull_durable_valid"] is True
    assert officer["auto_pull_mirror_consistent"] is True
    assert result["auto_pull_downgrade"]["safe"] is True


@pytest.mark.asyncio
async def test_malformed_auxiliary_submaps_do_not_obscure_valid_authority(db, visible):
    visible("all")
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

    result = await list_officers(MagicMock(), db, auto_pull_release_enabled=False)

    officer = result["officers"][0]
    assert officer["auto_pull_runtime_valid"] is True
    assert officer["model"] is None
    assert "pages_today" not in officer
    assert "digest_waiting" not in officer
    assert result["auto_pull_downgrade"]["safe"] is True


@pytest.mark.asyncio
async def test_ended_but_linked_true_mirror_blocks_downgrade(db, visible):
    visible("all")
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

    result = await list_officers(MagicMock(), db, auto_pull_release_enabled=False)

    assert result["officers"][0]["commissioned"] is False
    assert result["officers"][0]["auto_pull_runtime"] is True
    assert result["auto_pull_downgrade"]["safe"] is False
    assert result["auto_pull_downgrade"]["runtime_enabled"] == 1
    assert result["auto_pull_downgrade"]["mirror_mismatches"] == 1


@pytest.mark.asyncio
async def test_a_user_with_no_visible_projects_gets_an_empty_roster(db, visible):
    visible(set())

    assert (await list_officers(MagicMock(), db))["officers"] == []
    db.list_project_officer_posts.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_roster_never_creates_a_post(db, visible):
    visible({PROJECT_A})

    await list_officers(MagicMock(), db)

    db.get_or_create_project_officer.assert_not_awaited()
