"""Tests for ``orchestrator/services/automations.py``.

Covers the automation → job translation: prompt/expert mapping, autonomy
injection into ``config_override``, ``context.automation_id`` stamping
for the runs-view join, and trigger-kind propagation. DB is mocked —
this is the unit boundary for the helper.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.automations import create_job_from_automation


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
