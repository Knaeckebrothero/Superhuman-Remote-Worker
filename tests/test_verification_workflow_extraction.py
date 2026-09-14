from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from orchestrator.routers import verification as verification_routes
from orchestrator.services import verification_workflow


def _dependencies(store: MagicMock) -> verification_workflow.VerificationDependencies:
    transaction = verification_workflow.VerificationTransactionPorts(
        revalidate_datasource_selection=AsyncMock(return_value=([], {})),
        datasource_selection_provenance=AsyncMock(return_value={}),
        resolve_workspace_contract=MagicMock(
            return_value=SimpleNamespace(assigned_backend="sandbox")
        ),
        deep_merge_dicts=MagicMock(side_effect=lambda left, right: left | right),
        is_lite_config_override=MagicMock(return_value=False),
    )
    effects = verification_workflow.VerificationEffectPorts(
        forge=SimpleNamespace(is_initialized=False),
        notifier=MagicMock(),
        prepare_job_repository_authority=AsyncMock(),
        trigger_dispatch=MagicMock(),
        maybe_wake_session=AsyncMock(),
        kick_session_wake_drain=MagicMock(),
        trigger_curation_final_pass=AsyncMock(),
        set_target_to_autonomy_status=AsyncMock(return_value="completed"),
        escalate_target=AsyncMock(return_value="pending_review"),
        internal_resume_job=AsyncMock(return_value=True),
    )
    return verification_workflow.VerificationDependencies(
        store=store, transaction=transaction, effects=effects
    )


@pytest.mark.asyncio
async def test_duplicate_verdict_callback_replays_the_durable_round() -> None:
    target = {"id": "target", "context": {"verification_rounds": []}}
    critic = {
        "id": "critic",
        "context": {"verification_target": "target"},
    }
    store = MagicMock()
    store.get_job = AsyncMock(
        side_effect=lambda job_id: {"target": target, "critic": critic}[job_id]
    )

    async def append_round(_job_id: str, record: dict) -> int:
        target["context"]["verification_rounds"].append(record)
        return 1

    store.append_verification_round = AsyncMock(side_effect=append_round)
    store.increment_verdict_rejections = AsyncMock(return_value=1)
    dependencies = _dependencies(store)
    values = {
        "target_job_id": "target",
        "critic_job_id": "critic",
        "asserted_verdict": "approved",
        "opened": [],
        "dispositions": [],
        "head_commit": "abc",
        "dependencies": dependencies,
    }

    first = await verification_workflow.record_verification_round(**values)
    replay = await verification_workflow.record_verification_round(**values)

    assert first["verdict"] == "approved"
    assert replay == first
    store.append_verification_round.assert_awaited_once()


@pytest.mark.asyncio
async def test_verdict_for_an_unrelated_target_is_refused_before_append() -> None:
    target = {"id": "target", "context": {}}
    critic = {"id": "critic", "context": '{"verification_target":"other"}'}
    store = MagicMock()
    store.get_job = AsyncMock(
        side_effect=lambda job_id: {"target": target, "critic": critic}[job_id]
    )
    store.append_verification_round = AsyncMock()
    dependencies = _dependencies(store)

    with pytest.raises(HTTPException) as exc:
        await verification_workflow.record_verification_round(
            target_job_id="target",
            critic_job_id="critic",
            asserted_verdict="approved",
            opened=[],
            dispositions=[],
            head_commit=None,
            dependencies=dependencies,
        )

    assert exc.value.status_code == 403
    store.append_verification_round.assert_not_awaited()


@pytest.mark.asyncio
async def test_completion_decision_replay_does_not_repeat_the_row_write() -> None:
    decision = {"tool_call_id": "call-1", "summary": "Already durable"}
    store = MagicMock()
    store.get_job = AsyncMock(
        return_value={
            "id": "job-1",
            "status": "processing",
            "context": {"completion_decision": decision},
        }
    )
    store.set_completion_decision = AsyncMock(return_value=True)

    result = await verification_workflow.record_completion_decision(
        job_id="job-1",
        tool_call_id="call-1",
        summary="Retried payload",
        deliverables=[],
        confidence=1.0,
        notes=None,
        dependencies=_dependencies(store),
    )

    assert result == {"recorded": True, "replay": True, "decision": decision}
    store.set_completion_decision.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_commit_followup_is_inert_when_world_cas_lost() -> None:
    store = MagicMock()
    store.get_job = AsyncMock()
    dependencies = _dependencies(store)

    result = await verification_workflow.run_critic_verdict_followups(
        {"applicable": True, "world_cas_won": False},
        completion_command_id="command-1",
        dependencies=dependencies,
    )

    assert result == {"actions": []}
    store.get_job.assert_not_awaited()
    dependencies.effects.trigger_dispatch.assert_not_called()


def _client(
    dependencies: verification_workflow.VerificationDependencies,
    *,
    require_internal: AsyncMock | None = None,
) -> TestClient:
    application = FastAPI()
    route_dependencies = verification_routes.VerificationRouteDependencies(
        workflow=dependencies,
        require_internal=require_internal or AsyncMock(return_value=None),
    )
    application.state.verification_route_dependencies_factory = (
        lambda: route_dependencies
    )
    application.include_router(verification_routes.router)
    return TestClient(application, raise_server_exceptions=False)


def test_router_resolves_the_workflow_from_the_serving_application() -> None:
    first_store = MagicMock()
    first_store.get_job = AsyncMock(
        return_value={"context": {"completion_decision": {"summary": "first"}}}
    )
    second_store = MagicMock()
    second_store.get_job = AsyncMock(
        return_value={"context": {"completion_decision": {"summary": "second"}}}
    )
    first = _client(_dependencies(first_store))
    second = _client(_dependencies(second_store))

    assert first.get("/api/jobs/job/completion-decision").json() == {
        "decision": {"summary": "first"}
    }
    assert second.get("/api/jobs/job/completion-decision").json() == {
        "decision": {"summary": "second"}
    }
    assert first.get("/api/jobs/job/completion-decision").json() == {
        "decision": {"summary": "first"}
    }
