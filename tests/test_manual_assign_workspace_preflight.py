"""Manual assignment must preserve the dispatcher's workspace preflight."""

from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

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
        "execution_lane": "pinned",
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
        main.postgres_db,
        "prepare_pinned_job_for_workspace_resume",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        main.postgres_db, "claim_job_for_agent", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        main.postgres_db, "queue_job_for_resume", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(main, "_trigger_dispatch", MagicMock())
    monkeypatch.setattr(main, "_dispatch_job_to_agent", AsyncMock(return_value=True))
    monkeypatch.setattr(main, "_resume_job_on_agent", AsyncMock(return_value=True))


@pytest.mark.asyncio
async def test_flag_on_manual_assign_guard_blocks_before_workspace_or_agent_io(
    collaborators, monkeypatch
):
    job = _job("paused", workspace_status="failed")
    main.postgres_db.get_job.return_value = job
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    blocked = main.HTTPException(status_code=409, detail="completion finalizing")
    guard = AsyncMock(side_effect=blocked)
    monkeypatch.setattr(main, "_guard_completion_control", guard)

    with pytest.raises(main.HTTPException) as exc:
        await main.assign_job_to_agent(MagicMock(), JOB_ID, AGENT_ID)

    assert exc.value.status_code == 409
    guard.assert_awaited_once_with(JOB_ID, source="manual_assign")
    main.postgres_db.shed_workspace_context.assert_not_awaited()
    main.postgres_db.prepare_pinned_job_for_workspace_resume.assert_not_awaited()
    main.postgres_db.get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_on_missing_workspace_uses_claimed_atomic_preflight(
    collaborators, monkeypatch
):
    job = _job("failed", workspace_status="failed")
    main.postgres_db.get_job.return_value = job
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "_guard_completion_control", AsyncMock())
    claim = SimpleNamespace(claim_id="00000000-0000-0000-0000-000000000301")
    claim_control = AsyncMock(return_value=claim)
    monkeypatch.setattr(main, "_claim_completion_control", claim_control)

    result = await main.assign_job_to_agent(MagicMock(), JOB_ID, AGENT_ID)

    assert result["status"] == "queued"
    claim_control.assert_awaited_once_with(job, source="manual_assign_workspace")
    main.postgres_db.prepare_pinned_job_for_workspace_resume.assert_awaited_once_with(
        JOB_ID,
        "workspace_container",
        expected_status="failed",
        completion_control_claim_id=claim.claim_id,
    )
    main.postgres_db.shed_workspace_context.assert_not_awaited()
    main.postgres_db.queue_job_for_resume.assert_not_awaited()
    main.postgres_db.get_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_on_live_workspace_claims_before_agent_post(
    collaborators, monkeypatch
):
    job = _job("failed", workspace_status="ready")
    main.postgres_db.get_job.return_value = job
    main.postgres_db.get_agent.return_value = _agent()
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "_guard_completion_control", AsyncMock())
    order: list[str] = []
    main.postgres_db.claim_job_for_agent.side_effect = (
        lambda *_args, **_kwargs: order.append("claim") or True
    )
    main._dispatch_job_to_agent.side_effect = (
        lambda *_args, **_kwargs: order.append("post") or True
    )

    result = await main.assign_job_to_agent(MagicMock(), JOB_ID, AGENT_ID)

    assert result["status"] == "assigned"
    assert order == ["claim", "post"]
    main.postgres_db.claim_job_for_agent.assert_awaited_once_with(
        JOB_ID,
        AGENT_ID,
        completion_commands_enabled=True,
        allow_failed=True,
    )


class TestManualAssignWorkspacePreflight:
    @pytest.mark.asyncio
    async def test_stateless_job_rejects_direct_assignment(self, collaborators):
        job = _job("created", workspace_status="ready")
        job["execution_lane"] = "stateless"
        main.postgres_db.get_job.return_value = job

        with pytest.raises(main.HTTPException) as exc:
            await main.assign_job_to_agent(MagicMock(), JOB_ID, AGENT_ID)

        assert exc.value.status_code == 409
        main.postgres_db.get_agent.assert_not_awaited()
        main._dispatch_job_to_agent.assert_not_awaited()
        main._resume_job_on_agent.assert_not_awaited()

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


class TestAssignLaneChoice:
    """Paused jobs only take /job/resume when a checkpoint proves they ran.

    A paused-but-never-started job routed down /job/resume starts brief-less
    (knowledge-base/knowledge/issues/fresh_job_dispatched_as_resume_skips_seeding.md).
    """

    @pytest.mark.asyncio
    async def test_paused_with_checkpoint_uses_resume_lane(
        self, collaborators, monkeypatch
    ):
        monkeypatch.setattr(
            main.postgres_db, "job_has_checkpoint", AsyncMock(return_value=True)
        )
        main.postgres_db.get_job.return_value = _job("paused", workspace_status="ready")
        main.postgres_db.get_agent.return_value = _agent()

        result = await main.assign_job_to_agent(MagicMock(), JOB_ID, AGENT_ID)

        assert result["status"] == "assigned"
        main._resume_job_on_agent.assert_awaited_once()
        main._dispatch_job_to_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_paused_never_started_job_uses_the_fresh_lane(
        self, collaborators, monkeypatch
    ):
        monkeypatch.setattr(
            main.postgres_db, "job_has_checkpoint", AsyncMock(return_value=False)
        )
        main.postgres_db.get_job.return_value = _job("paused", workspace_status="ready")
        main.postgres_db.get_agent.return_value = _agent()

        result = await main.assign_job_to_agent(MagicMock(), JOB_ID, AGENT_ID)

        assert result["status"] == "assigned"
        main._dispatch_job_to_agent.assert_awaited_once()
        main._resume_job_on_agent.assert_not_awaited()


class TestPinnedDispatchDefenseInDepth:
    @pytest.mark.asyncio
    async def test_fresh_helper_refuses_stateless_job_before_network(self):
        job = _job("created", workspace_status="ready")
        job["execution_lane"] = "stateless"
        assert await main._dispatch_job_to_agent(job, _agent()) is False

    @pytest.mark.asyncio
    async def test_resume_helper_refuses_stateless_job_before_network(self):
        job = _job("paused", workspace_status="ready")
        job["execution_lane"] = "stateless"
        assert await main._resume_job_on_agent(job, _agent()) is False

    @pytest.mark.asyncio
    async def test_fresh_helper_refuses_redispatch_circuit_trip_before_network(self):
        job = _job("paused", workspace_status="ready")
        job["context"]["_lease_recovery"] = {
            "state": "tripped",
            "unchanged_recoveries": 3,
        }
        assert await main._dispatch_job_to_agent(job, _agent()) is False

    @pytest.mark.asyncio
    async def test_resume_helper_refuses_redispatch_circuit_trip_before_network(self):
        job = _job("paused", workspace_status="ready")
        job["context"]["_lease_recovery"] = {
            "state": "tripped",
            "unchanged_recoveries": 3,
        }
        assert await main._resume_job_on_agent(job, _agent()) is False
