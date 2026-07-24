"""Tests for ``orchestrator/services/automations.py``.

Covers the automation → job translation: prompt/expert mapping, autonomy
injection into ``config_override``, ``context.automation_id`` stamping
for the runs-view join, and trigger-kind propagation. DB is mocked —
this is the unit boundary for the helper.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.automations import (
    create_job_from_automation,
    validate_automation_expert_selection,
)
from orchestrator.services.default_experts import ExpertSelectionError


ROOT = Path(__file__).resolve().parents[1]


def _mock_db_returning_job(job_id: str = "j0000001-0000-0000-0000-000000000000"):
    db = MagicMock()
    db.create_job = AsyncMock(return_value={"id": job_id, "status": "created"})
    return db


def _make_automation_row(**overrides) -> dict:
    base = {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "owner_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "project_id": None,
        "name": "weekly-instagram-drafts",
        "expert": "scholar",
        "prompt": "Draft 3 Instagram posts about physiotherapy",
        "config_override": {},
        "autonomy": "review",
        "priority": 5,
    }
    base.update(overrides)
    return base


class TestCreateJobFromAutomation:
    @pytest.mark.asyncio
    async def test_passes_template_fields_to_create_job(self) -> None:
        db = _mock_db_returning_job()
        row = _make_automation_row()

        await create_job_from_automation(db, row, trigger_kind="cron")

        db.create_job.assert_awaited_once()
        kwargs = db.create_job.await_args.kwargs
        assert kwargs["description"] == row["prompt"]
        assert kwargs["config_name"] == row["expert"]
        assert kwargs["user_id"] == row["owner_id"]
        assert kwargs["priority"] == 5

    @pytest.mark.asyncio
    async def test_stamps_automation_context(self) -> None:
        db = _mock_db_returning_job()
        row = _make_automation_row()

        await create_job_from_automation(db, row, trigger_kind="cron")

        context = db.create_job.await_args.kwargs["context"]
        assert context["automation_id"] == row["id"]
        assert context["automation_name"] == row["name"]
        assert context["automation_trigger"] == "cron"

    @pytest.mark.asyncio
    async def test_injects_autonomy_into_config_override(self) -> None:
        """Jobs schema has no top-level autonomy — dispatch reads it from
        config_override.autonomy. The helper must inject it.
        """
        db = _mock_db_returning_job()
        row = _make_automation_row(autonomy="full")

        await create_job_from_automation(db, row)

        config_override = db.create_job.await_args.kwargs["config_override"]
        assert config_override["autonomy"] == "full"

    @pytest.mark.asyncio
    async def test_template_config_override_takes_precedence(self) -> None:
        """A template that already pins autonomy via config_override wins;
        the row-level autonomy default doesn't clobber it.
        """
        db = _mock_db_returning_job()
        row = _make_automation_row(
            autonomy="review",
            config_override={"autonomy": "full", "model": "claude-haiku"},
        )

        await create_job_from_automation(db, row)

        config_override = db.create_job.await_args.kwargs["config_override"]
        assert config_override["autonomy"] == "full"  # from override, not row
        assert config_override["model"] == "claude-haiku"

    @pytest.mark.asyncio
    async def test_trigger_kind_propagates(self) -> None:
        """run-now passes trigger_kind="manual" so audit shows the source."""
        db = _mock_db_returning_job()
        row = _make_automation_row()

        await create_job_from_automation(db, row, trigger_kind="manual")

        context = db.create_job.await_args.kwargs["context"]
        assert context["automation_trigger"] == "manual"

    @pytest.mark.asyncio
    async def test_project_id_passed_through(self) -> None:
        db = _mock_db_returning_job()
        row = _make_automation_row(project_id="cccccccc-cccc-cccc-cccc-cccccccccccc")

        await create_job_from_automation(db, row)

        assert (
            db.create_job.await_args.kwargs["project_id"]
            == "cccccccc-cccc-cccc-cccc-cccccccccccc"
        )

    @pytest.mark.asyncio
    async def test_unpinned_automation_resolves_application_worker_default(
        self, monkeypatch
    ) -> None:
        """Headless runs use the same DB default resolver as REST creation."""
        monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
        db = _mock_db_returning_job()
        owner_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        expert_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        db.get_user = AsyncMock(return_value={"id": owner_id, "is_admin": False})
        db.list_grants_for_scopes = AsyncMock(
            return_value={"user": [], "project": [], "global": []}
        )
        db.get_user_expert_default = AsyncMock(return_value=None)
        db.get_application_expert_default = AsyncMock(
            return_value={
                "id": expert_id,
                "expert_type": "worker",
                "owner_id": None,
            }
        )

        await create_job_from_automation(db, _make_automation_row(expert="worker_base"))

        kwargs = db.create_job.await_args.kwargs
        assert kwargs["config_name"] == "worker_base"
        assert kwargs["expert_id"] == expert_id
        assert kwargs["context"]["expert_selection"] == {
            "source": "application",
            "expert_id": expert_id,
        }

    @pytest.mark.asyncio
    async def test_pinned_db_expert_uses_explicit_expert_id(self, monkeypatch) -> None:
        monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
        db = _mock_db_returning_job()
        owner_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        expert_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        db.get_user = AsyncMock(return_value={"id": owner_id, "is_admin": False})
        db.get_expert_visible_by_id = AsyncMock(
            return_value={
                "id": expert_id,
                "expert_type": "worker",
                "owner_id": owner_id,
            }
        )

        await create_job_from_automation(
            db,
            _make_automation_row(expert="worker_base", expert_id=expert_id),
        )

        kwargs = db.create_job.await_args.kwargs
        assert kwargs["config_name"] == "worker_base"
        assert kwargs["expert_id"] == expert_id
        assert kwargs["context"]["expert_selection"] == {
            "source": "explicit",
            "expert_id": expert_id,
        }

    @pytest.mark.asyncio
    async def test_legacy_uuid_in_expert_column_is_recovered(self, monkeypatch) -> None:
        """Rows created by the old Cockpit still run before migration/backfill."""
        monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
        db = _mock_db_returning_job()
        owner_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        expert_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"
        db.get_user = AsyncMock(return_value={"id": owner_id, "is_admin": False})
        db.get_expert_visible_by_id = AsyncMock(
            return_value={
                "id": expert_id,
                "expert_type": "worker",
                "owner_id": owner_id,
            }
        )

        await create_job_from_automation(
            db,
            _make_automation_row(expert=expert_id),
        )

        kwargs = db.create_job.await_args.kwargs
        assert kwargs["config_name"] == "worker_base"
        assert kwargs["expert_id"] == expert_id


@pytest.mark.asyncio
async def test_automation_expert_validation_rejects_ambiguous_sources(
    monkeypatch,
) -> None:
    monkeypatch.setenv("EXPERTS_DB_ENABLED", "true")
    with pytest.raises(ExpertSelectionError, match="select one expert source"):
        await validate_automation_expert_selection(
            MagicMock(),
            owner_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            project_id=None,
            expert="scholar",
            expert_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        )


def test_automation_expert_id_migration_shape() -> None:
    sql = (
        ROOT / "orchestrator/database/migrations/app/0069_automation_expert_id.sql"
    ).read_text()
    assert "ADD COLUMN IF NOT EXISTS expert_id UUID" in sql
    assert "experts.expert_type = 'worker'" in sql
    assert "REFERENCES experts(id) ON DELETE RESTRICT" in sql
    assert "expert_id IS NULL OR expert = 'worker_base'" in sql
