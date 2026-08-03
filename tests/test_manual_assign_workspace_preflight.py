"""Manual assignment must preserve the dispatcher's workspace preflight."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import orchestrator.main as main


JOB_ID = "00000000-0000-0000-0000-000000000101"
AGENT_ID = "00000000-0000-0000-0000-000000000201"


def _job(status: str, *, workspace_status: str | None = None) -> dict:
    context = {}
    if workspace_status:
        context["workspace_container"] = {
            "status": workspace_status,
            **(
                {"host": "workspace-test.svc", "port": 30022}
                if workspace_status == "ready"
                else {}
            ),
        }
    return {
        "id": JOB_ID,
        "status": status,
        "config_override": {"workspace": {"backend": "sandbox"}},
        "context": context,
    }


def _agent() -> dict:
    return {
        "id": AGENT_ID,
        "status": "ready",
        "pod_ip": "10.42.0.9",
        "pod_port": 8080,
    }


@pytest.fixture
def collaborators(monkeypatch):
    monkeypatch.setattr(main, "_require_admin", AsyncMock())
    monkeypatch.setattr(main.postgres_db, "get_job", AsyncMock())
    monkeypatch.setattr(main.postgres_db, "get_agent", AsyncMock())
    monkeypatch.setattr(main.postgres_db, "shed_workspace_context", AsyncMock())
    monkeypatch.setattr(
        main.postgres_db, "queue_job_for_resume", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(main, "_trigger_dispatch", MagicMock())
    monkeypatch.setattr(main, "_dispatch_job_to_agent", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "_resume_job_on_agent", AsyncMock(return_value=True))


class TestManualAssignWorkspacePreflight:
    @pytest.mark.asyncio
    async def test_created_job_without_workspace_is_queued(self, collaborators):
        main.postgres_db.get_job.return_value = _job("created")

        result = await main.assign_job_to_agent(MagicMock(), JOB_ID, AGENT_ID)

        assert result["status"] == "queued"
        assert "not reserved" in result["message"]
        main.postgres_db.shed_workspace_context.assert_awaited_once_with(
            JOB_ID, "workspace_container"
        )
        main.postgres_db.queue_job_for_resume.assert_not_awaited()
        main.postgres_db.get_agent.assert_not_awaited()
        main._dispatch_job_to_agent.assert_not_awaited()
        main._trigger_dispatch.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_failed_job_without_workspace_is_made_dispatchable(
        self, collaborators
    ):
        main.postgres_db.get_job.return_value = _job(
            "failed", workspace_status="failed"
        )

        result = await main.assign_job_to_agent(MagicMock(), JOB_ID, AGENT_ID)

        assert result["status"] == "queued"
        main.postgres_db.queue_job_for_resume.assert_awaited_once_with(JOB_ID)
        main.postgres_db.get_agent.assert_not_awaited()
        main._trigger_dispatch.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_live_workspace_still_allows_direct_admin_override(
        self, collaborators
    ):
        main.postgres_db.get_job.return_value = _job(
            "created", workspace_status="ready"
        )
        main.postgres_db.get_agent.return_value = _agent()

        result = await main.assign_job_to_agent(MagicMock(), JOB_ID, AGENT_ID)

        assert result == {
            "status": "assigned",
            "agent_id": AGENT_ID,
            "job_id": JOB_ID,
        }
        main.postgres_db.shed_workspace_context.assert_not_awaited()
        main._dispatch_job_to_agent.assert_awaited_once()
        main._trigger_dispatch.assert_not_called()
