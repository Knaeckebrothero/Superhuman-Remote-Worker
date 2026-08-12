"""Adversarial driver tests for stateless worker batch dispositions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

import src.api.persistent_app as pa
import src.api.turn_executor as turn_executor
from src.agent import UniversalAgent, _stateless_worker_remote_authority
from src.core.workspace_backend import WorkspaceUnavailableError
from src.graph import route_entry
from src.shared.run_queue import ClaimedUnit, EnqueueResult
from src.shared.job_steering import CheckpointSteeringAcker, context_delivery_key
from src.shared.worker_queue import (
    WorkerClaim,
    WorkerCompletionAcceptance,
    WorkerRenewal,
    WorkerRotation,
)

WORKSPACE_GENERATION = "11111111-1111-4111-8111-111111111111"
WORKSPACE_RUNTIME = "22222222-2222-4222-8222-222222222222"
WORKSPACE_FINGERPRINT = "SHA256:" + ("A" * 43)


def _claim(
    *,
    token=7,
    input_seq=None,
    prior="created",
    attempts=1,
    max_attempts=5,
) -> WorkerClaim:
    unit = ClaimedUnit(
        unit_id=uuid4(),
        unit_kind="worker_batch",
        fair_key="user-a",
        lease_token=token,
        input_seq=input_seq,
        consumed_seq=None,
        attempts_since_completion=attempts,
        leased_until=datetime.now(timezone.utc),
    )
    return WorkerClaim(
        unit=unit,
        prior_job_status=prior,
        resume=prior != "created",
        max_attempts=max_attempts,
    )


def _renewal(status="processing") -> WorkerRenewal:
    return WorkerRenewal(
        leased_until=datetime.now(timezone.utc),
        job_status=status,
        job_context={},
        pending_guidance=(),
        queued_replies=(),
    )


def _bundle(claim: WorkerClaim) -> dict:
    job_id = str(claim.unit_id)
    return {
        "unit_id": job_id,
        "job_id": job_id,
        "unit_kind": "worker_batch",
        "execution_lane": "stateless",
        "job": {
            "job_id": job_id,
            "description": "continue the task",
            "config_name": "worker_base",
            "context": {},
            "workspace_generation": WORKSPACE_GENERATION,
            "workspace_runtime_incarnation": WORKSPACE_RUNTIME,
            "workspace_ssh_host_key_fingerprint": WORKSPACE_FINGERPRINT,
            "workspace_owner_kind": "job",
            "workspace_owner_id": job_id,
        },
        "batch": {
            "target_wall_seconds": 60.0,
            "min_wall_seconds": 0.0,
            "iteration_cap": 3,
        },
    }


def test_worker_bundle_preserves_exact_workspace_authority_in_metadata():
    claim = _claim()
    request, _ = turn_executor.StatelessTurnExecutor._parse_worker_bundle(
        _bundle(claim), claim
    )

    metadata = turn_executor.StatelessTurnExecutor._worker_job_metadata(request)

    assert metadata["workspace_generation"] == WORKSPACE_GENERATION
    assert metadata["workspace_runtime_incarnation"] == WORKSPACE_RUNTIME
    assert metadata["workspace_ssh_host_key_fingerprint"] == WORKSPACE_FINGERPRINT
    assert metadata["workspace_owner_kind"] == "job"
    assert metadata["workspace_owner_id"] == str(claim.unit.unit_id)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("workspace_generation", None),
        ("workspace_runtime_incarnation", "not-a-uuid"),
        ("workspace_ssh_host_key_fingerprint", "SHA256:short"),
        ("workspace_owner_kind", "session"),
        ("workspace_owner_id", "NOT-CANONICAL"),
    ],
)
def test_worker_bundle_rejects_missing_or_malformed_workspace_authority(
    field, replacement
):
    claim = _claim()
    bundle = _bundle(claim)
    if replacement is None:
        bundle["job"].pop(field)
    else:
        bundle["job"][field] = replacement

    with pytest.raises(ValueError):
        turn_executor.StatelessTurnExecutor._parse_worker_bundle(bundle, claim)


def test_agent_worker_authority_maps_all_remote_backend_fields():
    metadata = {
        "workspace_generation": WORKSPACE_GENERATION,
        "workspace_runtime_incarnation": WORKSPACE_RUNTIME,
        "workspace_ssh_host_key_fingerprint": WORKSPACE_FINGERPRINT,
        "workspace_owner_kind": "job",
        "workspace_owner_id": "33333333-3333-4333-8333-333333333333",
    }

    assert _stateless_worker_remote_authority(metadata, 7) == {
        "workspace_generation": WORKSPACE_GENERATION,
        "runtime_incarnation": WORKSPACE_RUNTIME,
        "expected_host_key_fingerprint": WORKSPACE_FINGERPRINT,
        "workspace_owner_kind": "job",
        "workspace_owner_id": "33333333-3333-4333-8333-333333333333",
    }
    assert _stateless_worker_remote_authority({}, None) == {}


@pytest.mark.parametrize(
    "mutation",
    [
        {"workspace_generation": None},
        {"workspace_runtime_incarnation": None},
        {"workspace_ssh_host_key_fingerprint": None},
        {"workspace_owner_kind": "session"},
        {"workspace_owner_id": None},
    ],
)
def test_agent_worker_authority_fails_closed_on_incomplete_or_non_job_owner(mutation):
    metadata = {
        "workspace_generation": WORKSPACE_GENERATION,
        "workspace_runtime_incarnation": WORKSPACE_RUNTIME,
        "workspace_ssh_host_key_fingerprint": WORKSPACE_FINGERPRINT,
        "workspace_owner_kind": "job",
        "workspace_owner_id": "33333333-3333-4333-8333-333333333333",
    }
    metadata.update(mutation)

    with pytest.raises(WorkspaceUnavailableError):
        _stateless_worker_remote_authority(metadata, 7)


class _FakeAgent:
    def __init__(self, final_state):
        self.postgres_conn = object()
        self.final_state = final_state
        self.process_calls = []
        self.cleanup_calls = []
        self._orchestrator_client = None

    async def process_job(self, *args, **kwargs):
        self.process_calls.append((args, kwargs))
        final_state = self.final_state

        async def stream():
            yield final_state

        return stream()

    async def cleanup_worker_claim(self, *, preserve_shell):
        self.cleanup_calls.append(preserve_shell)


class _FakeClient:
    def __init__(self, claim, *, report_result=True):
        self.claim = claim
        self.report_result = report_result
        self.report_completion = AsyncMock(return_value=report_result)

    async def get_claim_bundle(self, unit_id, lease_token):
        assert (unit_id, lease_token) == (
            str(self.claim.unit_id),
            self.claim.lease_token,
        )
        return _bundle(self.claim)


@pytest.fixture
def worker_runtime(monkeypatch):
    saved = {
        "agent": pa._agent,
        "client": pa._orchestrator_client,
        "session": pa._session,
        "thread_id": pa._thread_id,
    }
    pa._session = None
    pa._thread_id = None
    monkeypatch.setattr(
        turn_executor.StatelessTurnExecutor,
        "_scrub_process_residue",
        lambda _self: None,
    )
    try:
        yield
    finally:
        pa._agent = saved["agent"]
        pa._orchestrator_client = saved["client"]
        pa._session = saved["session"]
        pa._thread_id = saved["thread_id"]


def _install(monkeypatch, claim, final_state, *, report_result=True):
    agent = _FakeAgent(final_state)
    client = _FakeClient(claim, report_result=report_result)
    pa._agent = agent
    pa._orchestrator_client = client
    renew = AsyncMock(return_value=_renewal())
    rotate = AsyncMock(
        return_value=WorkerRotation(
            completed_state="queued",
            enqueue=EnqueueResult(status="input_recorded", state="leased"),
            prior_input_seq=claim.unit.input_seq,
            next_input_seq=int(claim.unit.input_seq or 0) + 1,
        )
    )
    complete = AsyncMock(return_value="done")
    release = AsyncMock(return_value="queued")
    monkeypatch.setattr(turn_executor, "renew_worker_batch", renew)
    monkeypatch.setattr(turn_executor, "rotate_worker_batch", rotate)
    monkeypatch.setattr(turn_executor, "complete_worker_batch", complete)
    monkeypatch.setattr(turn_executor, "release_worker_batch", release)
    executor = turn_executor.StatelessTurnExecutor(
        pod_name="worker-pod",
        worker_enabled=True,
    )
    return executor, agent, client, renew, rotate, complete, release


@pytest.mark.asyncio
async def test_rotation_uses_complete_and_requeue_and_zero_complete_calls(
    worker_runtime, monkeypatch, caplog
):
    claim = _claim(input_seq=12, prior="processing")
    final = {
        "should_stop": True,
        "freeze_data": {"freeze_type": "batch_boundary"},
        "error": None,
    }
    executor, agent, client, _, rotate, complete, release = _install(
        monkeypatch, claim, final
    )

    with caplog.at_level("INFO"):
        await executor._serve_worker_claim(claim)

    client.report_completion.assert_not_awaited()
    rotate.assert_awaited_once_with(
        executor._db,
        unit_id=claim.unit_id,
        lease_token=claim.lease_token,
        input_seq=12,
        fair_key="user-a",
    )
    complete.assert_not_awaited()
    release.assert_not_awaited()
    assert agent.cleanup_calls == [True]
    post_commit = agent.process_calls[0][1]["worker_checkpoint_post_commit"]
    assert isinstance(post_commit, CheckpointSteeringAcker)
    assert post_commit.job_id == str(claim.unit_id)
    assert post_commit.client is client
    assert "queue_verb=complete_and_requeue" in caplog.text
    assert "complete_calls=0" in caplog.text
    assert "http_complete_calls=0" in caplog.text


@pytest.mark.asyncio
async def test_terminal_reports_once_then_closes_exact_watermark(
    worker_runtime, monkeypatch
):
    claim = _claim(input_seq=31, prior="processing")
    final = {
        "should_stop": True,
        "goal_achieved": True,
        "freeze_data": None,
        "error": None,
    }
    executor, agent, client, _, rotate, complete, release = _install(
        monkeypatch, claim, final
    )

    await executor._serve_worker_claim(claim)

    client.report_completion.assert_awaited_once_with(
        str(claim.unit_id), final, lease_token=claim.lease_token
    )
    complete.assert_awaited_once_with(
        executor._db,
        unit_id=claim.unit_id,
        lease_token=claim.lease_token,
        consumed_seq=31,
    )
    rotate.assert_not_awaited()
    release.assert_not_awaited()
    assert agent.cleanup_calls == [False]


@pytest.mark.asyncio
@pytest.mark.parametrize("report_result", [True, False])
async def test_command_accept_queue_closure_is_not_misclassified_as_lease_loss(
    worker_runtime,
    monkeypatch,
    report_result,
):
    """B4 closes the queue inside accept, before the HTTP response returns."""

    claim = _claim(input_seq=31, prior="processing")
    final = {
        "should_stop": True,
        "goal_achieved": True,
        "freeze_data": None,
        "error": None,
    }
    executor, agent, client, renew, rotate, complete, release = _install(
        monkeypatch,
        claim,
        final,
        report_result=report_result,
    )
    executor._completion_commands_enabled = True
    renew.side_effect = [_renewal("processing"), None]
    acceptance = WorkerCompletionAcceptance(
        job_status="completed",
        queue_state="done",
        command_state="done" if report_result else "pending",
        command_id=uuid4(),
    )
    accepted_lookup = AsyncMock(return_value=acceptance)
    monkeypatch.setattr(
        turn_executor,
        "get_worker_completion_acceptance",
        accepted_lookup,
    )

    await executor._serve_worker_claim(claim)

    client.report_completion.assert_awaited_once()
    accepted_lookup.assert_awaited_once_with(
        executor._db,
        unit_id=claim.unit_id,
        lease_token=claim.lease_token,
    )
    complete.assert_not_awaited()
    release.assert_not_awaited()
    rotate.assert_not_awaited()
    assert executor._lease.lost.is_set() is False
    assert agent.cleanup_calls == ([False] if report_result else [True])


@pytest.mark.asyncio
async def test_recoverable_end_releases_without_reporting(worker_runtime, monkeypatch):
    claim = _claim(input_seq=4, prior="processing")
    final = {
        "should_stop": True,
        "freeze_data": None,
        "error": {"type": "infra_transient", "recoverable": True},
    }
    executor, agent, client, _, rotate, complete, release = _install(
        monkeypatch, claim, final
    )

    await executor._serve_worker_claim(claim)

    client.report_completion.assert_not_awaited()
    release.assert_awaited_once_with(
        executor._db,
        unit_id=claim.unit_id,
        lease_token=claim.lease_token,
        park_on_exhaustion=True,
    )
    rotate.assert_not_awaited()
    complete.assert_not_awaited()
    assert agent.cleanup_calls == [True]


@pytest.mark.asyncio
async def test_last_recoverable_attempt_reports_visible_terminal_give_up(
    worker_runtime, monkeypatch
):
    claim = _claim(input_seq=4, prior="processing", attempts=5, max_attempts=5)
    final = {
        "should_stop": True,
        "freeze_data": {
            "freeze_type": "llm_unavailable",
            "reason": "endpoint offline",
        },
        "error": {"type": "llm_unavailable", "recoverable": True},
    }
    executor, agent, client, _, rotate, complete, release = _install(
        monkeypatch, claim, final
    )

    await executor._serve_worker_claim(claim)

    client.report_completion.assert_awaited_once()
    reported = client.report_completion.await_args.args[1]
    assert reported["error"]["type"] == "worker_retry_exhausted"
    assert reported["error"]["recoverable"] is False
    assert reported["freeze_data"]["freeze_type"] == "worker_retry_exhausted"
    assert reported["freeze_data"]["attempts"] == 5
    complete.assert_awaited_once()
    rotate.assert_not_awaited()
    release.assert_not_awaited()
    assert agent.cleanup_calls == [False]


@pytest.mark.asyncio
async def test_last_pregraph_driver_failure_reports_visible_terminal_give_up(
    worker_runtime, monkeypatch
):
    claim = _claim(input_seq=4, prior="processing", attempts=5, max_attempts=5)
    executor, agent, client, _, rotate, complete, release = _install(
        monkeypatch,
        claim,
        {
            "should_stop": True,
            "error": {"type": "infra_transient", "recoverable": True},
        },
    )
    client.get_claim_bundle = AsyncMock(
        side_effect=RuntimeError("bundle credentials were revoked")
    )

    await executor._serve_worker_claim(claim)

    client.report_completion.assert_awaited_once()
    reported = client.report_completion.await_args.args[1]
    assert reported["error"]["type"] == "worker_retry_exhausted"
    assert reported["error"]["recoverable"] is False
    assert reported["freeze_data"]["prior_freeze"] is None
    complete.assert_awaited_once_with(
        executor._db,
        unit_id=claim.unit_id,
        lease_token=claim.lease_token,
        consumed_seq=4,
    )
    rotate.assert_not_awaited()
    release.assert_not_awaited()
    assert agent.cleanup_calls == [False]


@pytest.mark.asyncio
async def test_failed_max_give_up_successor_re_reports_without_running_work(
    worker_runtime, monkeypatch
):
    first = _claim(input_seq=4, prior="processing", attempts=5, max_attempts=5)
    recoverable = {
        "should_stop": True,
        "freeze_data": {"freeze_type": "llm_unavailable"},
        "error": {"type": "llm_unavailable", "recoverable": True},
    }
    executor, agent, client, _, rotate, complete, release = _install(
        monkeypatch,
        first,
        recoverable,
    )
    client.report_completion.side_effect = [False, True]
    client.get_claim_bundle = AsyncMock(return_value=_bundle(first))

    await executor._serve_worker_claim(first)
    successor = WorkerClaim(
        unit=replace(first.unit, lease_token=8, attempts_since_completion=6),
        prior_job_status="processing",
        resume=True,
        max_attempts=5,
    )
    client.claim = successor
    await executor._serve_worker_claim(successor)

    assert client.report_completion.await_count == 2
    assert client.get_claim_bundle.await_count == 2
    assert len(agent.process_calls) == 2
    assert agent.process_calls[1][1]["worker_retry_exhausted"] is True
    for call in client.report_completion.await_args_list:
        assert call.args[1]["error"]["type"] == "worker_retry_exhausted"
    release.assert_awaited_once_with(
        executor._db,
        unit_id=first.unit_id,
        lease_token=first.lease_token,
        park_on_exhaustion=False,
    )
    complete.assert_awaited_once_with(
        executor._db,
        unit_id=successor.unit_id,
        lease_token=successor.lease_token,
        consumed_seq=4,
    )
    rotate.assert_not_awaited()


@pytest.mark.asyncio
async def test_over_budget_claim_detaches_warm_session_before_terminal_report(
    worker_runtime, monkeypatch
):
    claim = _claim(input_seq=4, prior="processing", attempts=6, max_attempts=5)
    executor, agent, client, _, rotate, complete, release = _install(
        monkeypatch,
        claim,
        {
            "should_stop": True,
            "error": {"type": "infra_transient", "recoverable": True},
        },
    )
    events: list[str] = []
    pa._session = object()

    async def detach(reason):
        assert reason == "worker_claim_switch"
        events.append("detached")
        pa._session = None

    async def report(*args, **kwargs):
        events.append("reported")
        return True

    executor._detach_cached_session = AsyncMock(side_effect=detach)
    client.report_completion.side_effect = report
    client.get_claim_bundle = AsyncMock(return_value=_bundle(claim))

    await executor._serve_worker_claim(claim)

    assert events[:2] == ["detached", "reported"]
    executor._detach_cached_session.assert_awaited_once_with("worker_claim_switch")
    client.get_claim_bundle.assert_awaited_once()
    assert len(agent.process_calls) == 1
    assert agent.process_calls[0][1]["worker_retry_exhausted"] is True
    client.report_completion.assert_awaited_once()
    complete.assert_awaited_once()
    rotate.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_over_budget_successor_re_reports_genuine_end_unchanged(
    worker_runtime, monkeypatch
):
    first = _claim(input_seq=11, prior="processing", attempts=5, max_attempts=5)
    terminal = {
        "should_stop": True,
        "goal_achieved": True,
        "freeze_data": None,
        "error": None,
    }
    executor, agent, client, _, rotate, complete, release = _install(
        monkeypatch,
        first,
        terminal,
    )
    client.report_completion.side_effect = [False, True]

    await executor._serve_worker_claim(first)
    successor = WorkerClaim(
        unit=replace(first.unit, lease_token=8, attempts_since_completion=6),
        prior_job_status="processing",
        resume=True,
        max_attempts=5,
    )
    client.claim = successor
    await executor._serve_worker_claim(successor)

    assert client.report_completion.await_count == 2
    assert [call.args[1] for call in client.report_completion.await_args_list] == [
        terminal,
        terminal,
    ]
    assert len(agent.process_calls) == 2
    assert agent.process_calls[1][1]["worker_retry_exhausted"] is True
    release.assert_awaited_once_with(
        executor._db,
        unit_id=first.unit_id,
        lease_token=first.lease_token,
        park_on_exhaustion=False,
    )
    complete.assert_awaited_once_with(
        executor._db,
        unit_id=successor.unit_id,
        lease_token=successor.lease_token,
        consumed_seq=11,
    )
    rotate.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_stream_at_max_reports_give_up_instead_of_parking(
    worker_runtime, monkeypatch
):
    claim = _claim(input_seq=3, prior="processing", attempts=5, max_attempts=5)
    executor, agent, client, _, rotate, complete, release = _install(
        monkeypatch,
        claim,
        {"should_stop": False},
    )

    async def empty_stream():
        if False:  # pragma: no cover - makes this an async generator
            yield {}

    agent.process_job = AsyncMock(return_value=empty_stream())

    await executor._serve_worker_claim(claim)

    client.report_completion.assert_awaited_once()
    reported = client.report_completion.await_args.args[1]
    assert reported["error"]["type"] == "worker_retry_exhausted"
    assert "without a durable terminal state" in reported["error"]["message"]
    complete.assert_awaited_once()
    rotate.assert_not_awaited()
    release.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_report_tail_failure_never_reports_twice_or_parks_at_max(
    worker_runtime, monkeypatch
):
    claim = _claim(input_seq=6, prior="processing", attempts=5, max_attempts=5)
    final = {"should_stop": True, "goal_achieved": True}
    executor, agent, client, renew, rotate, complete, release = _install(
        monkeypatch,
        claim,
        final,
    )
    renew.side_effect = [
        _renewal("processing"),
        RuntimeError("post-report renewal transient"),
    ]

    await executor._serve_worker_claim(claim)

    client.report_completion.assert_awaited_once_with(
        str(claim.unit_id), final, lease_token=claim.lease_token
    )
    complete.assert_not_awaited()
    rotate.assert_not_awaited()
    release.assert_awaited_once_with(
        executor._db,
        unit_id=claim.unit_id,
        lease_token=claim.lease_token,
        park_on_exhaustion=False,
    )
    assert agent.cleanup_calls == [True]


@pytest.mark.asyncio
async def test_terminal_report_failure_preserves_shell_and_error_releases(
    worker_runtime, monkeypatch
):
    claim = _claim(input_seq=9, prior="processing")
    final = {"should_stop": True, "goal_achieved": True}
    executor, agent, client, _, _, complete, release = _install(
        monkeypatch,
        claim,
        final,
        report_result=False,
    )

    await executor._serve_worker_claim(claim)

    client.report_completion.assert_awaited_once()
    assert agent.cleanup_calls == [True]
    complete.assert_not_awaited()
    release.assert_awaited_once_with(
        executor._db,
        unit_id=claim.unit_id,
        lease_token=claim.lease_token,
        park_on_exhaustion=False,
    )


@pytest.mark.asyncio
async def test_cancel_winning_during_terminal_report_closes_without_error_release(
    worker_runtime,
    monkeypatch,
    caplog,
):
    claim = _claim(input_seq=10, prior="processing")
    final = {"should_stop": True, "goal_achieved": True}
    executor, agent, client, renew, _, complete, release = _install(
        monkeypatch,
        claim,
        final,
        report_result=False,
    )
    renew.side_effect = [_renewal("processing"), _renewal("cancelled")]

    with caplog.at_level("INFO"):
        await executor._serve_worker_claim(claim)

    client.report_completion.assert_awaited_once()
    complete.assert_awaited_once_with(
        executor._db,
        unit_id=claim.unit_id,
        lease_token=claim.lease_token,
        consumed_seq=10,
    )
    release.assert_not_awaited()
    assert agent.cleanup_calls == [False]
    assert "status=cancelled" in caplog.text
    assert "http_complete_calls=1" in caplog.text


@pytest.mark.asyncio
async def test_terminal_report_failure_reclaims_end_and_second_report_closes_queue(
    worker_runtime, monkeypatch
):
    first = _claim(token=21, input_seq=9, prior="processing")
    final = {"should_stop": True, "goal_achieved": True, "error": None}
    executor, agent, client, _, _, complete, release = _install(
        monkeypatch,
        first,
        final,
    )
    client.report_completion.side_effect = [False, True]

    await executor._serve_worker_claim(first)
    successor = WorkerClaim(
        unit=replace(first.unit, lease_token=22, attempts_since_completion=2),
        prior_job_status="processing",
        resume=True,
    )
    client.claim = successor
    await executor._serve_worker_claim(successor)

    assert client.report_completion.await_count == 2
    assert [
        call.kwargs["lease_token"] for call in client.report_completion.await_args_list
    ] == [
        21,
        22,
    ]
    release.assert_awaited_once_with(
        executor._db,
        unit_id=first.unit_id,
        lease_token=21,
        park_on_exhaustion=False,
    )
    complete.assert_awaited_once_with(
        executor._db,
        unit_id=successor.unit_id,
        lease_token=22,
        consumed_seq=9,
    )
    assert agent.cleanup_calls == [True, False]


@pytest.mark.asyncio
async def test_claim_time_pause_preempts_with_zero_report_calls(
    worker_runtime, monkeypatch
):
    claim = _claim(input_seq=5, prior="processing")
    executor, agent, client, renew, _, complete, release = _install(
        monkeypatch,
        claim,
        {"should_stop": False},
    )
    renew.return_value = _renewal("paused")

    await executor._serve_worker_claim(claim)

    assert agent.process_calls == []
    client.report_completion.assert_not_awaited()
    assert agent.cleanup_calls == [True]
    complete.assert_awaited_once_with(
        executor._db,
        unit_id=claim.unit_id,
        lease_token=claim.lease_token,
        consumed_seq=5,
    )
    release.assert_not_awaited()


class TestWorkerBatchArming:
    def test_worker_claim_hydrates_checkpointed_instruction_reads(self):
        context = SimpleNamespace(
            restore_instruction_read_receipts=MagicMock(return_value=1)
        )
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._tool_context = context
        receipts = {
            "skills/verify-before-done/SKILL.md": {
                "phase": "tactical",
                "phase_number": 1,
                "turn_count": 4,
            }
        }

        assert (
            agent._restore_worker_instruction_reads(
                {"instruction_read_receipts": receipts}
            )
            == 1
        )
        context.restore_instruction_read_receipts.assert_called_once_with(receipts)

    @pytest.mark.asyncio
    async def test_fresh_stateless_resume_injects_feedback_before_first_checkpoint(
        self,
    ):
        graph = SimpleNamespace(aupdate_state=AsyncMock())
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._graph = graph
        graph_input = {"messages": [], "should_stop": True}
        checkpoint_values = {}
        delivery_id = "b5426cab-66d8-48e6-bf30-9027fe4602b4"

        await agent._inject_resume_feedback(
            job_id=str(uuid4()),
            stateless_worker=True,
            graph_input=graph_input,
            thread_config={"configurable": {"thread_id": "job"}},
            checkpoint_values=checkpoint_values,
            feedback="continue from the durable request",
            feedback_reason="reviewer resumed",
            metadata={"queued_feedback_delivery_id": delivery_id},
        )

        expected_key = context_delivery_key(
            "feedback",
            "continue from the durable request",
            delivery_id=delivery_id,
            companion="reviewer resumed",
        )
        assert graph_input["delivered_feedback_keys"] == [expected_key]
        assert graph_input["should_stop"] is False
        assert graph_input["client_report_id"] is None
        assert graph_input["completion_report_payload"] is None
        assert route_entry(graph_input) == "init_workspace"
        assert len(graph_input["messages"]) == 1
        assert graph_input["client_report_id"] is None
        assert graph_input["completion_report_payload"] is None
        assert "continue from the durable request" in graph_input["messages"][0].content
        assert "reviewer resumed" in graph_input["messages"][0].content
        assert checkpoint_values["delivered_feedback_keys"] == [expected_key]
        graph.aupdate_state.assert_not_awaited()

        client = AsyncMock()
        client.ack_job_guidance.return_value = True
        acker = CheckpointSteeringAcker("job", client)
        assert await acker.reconcile_values(graph_input, checkpoint_id="cp-first")
        client.ack_job_guidance.assert_awaited_once_with(
            "job",
            guidance_ids=[],
            reply_keys=[],
            feedback_keys=[expected_key],
            delegation_keys=[],
            checkpoint_id="cp-first",
        )

    @pytest.mark.asyncio
    async def test_fresh_stateless_resume_injects_delegation_before_checkpoint(self):
        graph = SimpleNamespace(aupdate_state=AsyncMock())
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._graph = graph
        graph_input = {"messages": []}
        checkpoint_values = {}
        results = [{"job_id": "child-1", "status": "completed"}]
        delivery_id = "ee8193cc-bc57-49e6-978c-622f47d0a462"

        await agent._inject_delegation_results(
            job_id=str(uuid4()),
            stateless_worker=True,
            graph_input=graph_input,
            thread_config={"configurable": {"thread_id": "job"}},
            checkpoint_values=checkpoint_values,
            delegation_results=results,
            metadata={"delegation_results_delivery_id": delivery_id},
        )

        expected_key = context_delivery_key(
            "delegation",
            results,
            delivery_id=delivery_id,
        )
        assert graph_input["delivered_delegation_keys"] == [expected_key]
        assert len(graph_input["messages"]) == 1
        assert "child-1" in graph_input["messages"][0].content
        assert checkpoint_values["delivered_delegation_keys"] == [expected_key]
        graph.aupdate_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mid_loop_update_preserves_pending_frontier(self):
        graph = SimpleNamespace(
            aget_state=AsyncMock(
                return_value=SimpleNamespace(
                    values={"iteration": 8, "should_stop": False},
                    next=("audited_tools",),
                )
            ),
            aupdate_state=AsyncMock(),
        )
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._graph = graph

        terminal = await agent._arm_worker_batch(
            job_id=str(uuid4()),
            graph_input=None,
            thread_config={"configurable": {"thread_id": "job"}},
            target_wall_seconds=60,
            min_wall_seconds=0,
            iteration_cap=10,
        )

        graph.aupdate_state.assert_awaited_once()
        assert graph.aupdate_state.await_args.kwargs == {}
        assert terminal is None

    @pytest.mark.asyncio
    async def test_recoverable_end_reenters_through_start_and_clears_error(self):
        graph = SimpleNamespace(
            aget_state=AsyncMock(
                return_value=SimpleNamespace(
                    values={
                        "iteration": 8,
                        "should_stop": True,
                        "error": {"recoverable": True},
                        "freeze_data": None,
                        "client_report_id": "11111111-1111-4111-8111-111111111111",
                        "completion_report_payload": {
                            "should_stop": True,
                            "goal_achieved": False,
                            "error": {"recoverable": True},
                            "freeze_data": None,
                        },
                    },
                    next=(),
                )
            ),
            aupdate_state=AsyncMock(),
        )
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._graph = graph

        terminal = await agent._arm_worker_batch(
            job_id=str(uuid4()),
            graph_input=None,
            thread_config={"configurable": {"thread_id": "job"}},
            target_wall_seconds=60,
            min_wall_seconds=0,
            iteration_cap=10,
        )

        updates = graph.aupdate_state.await_args.args[1]
        assert graph.aupdate_state.await_args.kwargs == {"as_node": "__start__"}
        assert updates["error"] is None
        assert updates["should_stop"] is False
        assert updates["client_report_id"] is None
        assert updates["completion_report_payload"] is None
        assert terminal is None

    @pytest.mark.asyncio
    async def test_terminal_end_is_not_rearmed_or_reentered(self):
        graph = SimpleNamespace(
            aget_state=AsyncMock(
                return_value=SimpleNamespace(
                    values={
                        "iteration": 8,
                        "should_stop": True,
                        "goal_achieved": True,
                        "error": None,
                        "client_report_id": "11111111-1111-4111-8111-111111111111",
                        "completion_report_payload": {
                            "should_stop": True,
                            "goal_achieved": True,
                            "error": None,
                            "freeze_data": None,
                        },
                    },
                    next=(),
                )
            ),
            aupdate_state=AsyncMock(),
        )
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._graph = graph

        terminal = await agent._arm_worker_batch(
            job_id=str(uuid4()),
            graph_input=None,
            thread_config={"configurable": {"thread_id": "job"}},
            target_wall_seconds=60,
            min_wall_seconds=0,
            iteration_cap=10,
        )

        graph.aupdate_state.assert_not_awaited()
        assert terminal["goal_achieved"] is True
        assert terminal["client_report_id"] == ("11111111-1111-4111-8111-111111111111")

    @pytest.mark.asyncio
    async def test_exhausted_probe_preserves_terminal_end_without_graph_update(self):
        values = {
            "iteration": 8,
            "should_stop": True,
            "goal_achieved": True,
            "freeze_data": None,
            "error": None,
        }
        graph = SimpleNamespace(
            aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=())),
            aupdate_state=AsyncMock(),
        )
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._graph = graph

        terminal = await agent._arm_worker_batch(
            job_id=str(uuid4()),
            graph_input=None,
            thread_config={"configurable": {"thread_id": "job"}},
            target_wall_seconds=60,
            min_wall_seconds=0,
            iteration_cap=10,
            retry_exhausted=True,
        )

        graph.aupdate_state.assert_not_awaited()
        assert terminal == values

    @pytest.mark.asyncio
    async def test_exhausted_probe_suppresses_recoverable_end_reentry(self):
        graph = SimpleNamespace(
            aget_state=AsyncMock(
                return_value=SimpleNamespace(
                    values={
                        "iteration": 8,
                        "should_stop": True,
                        "freeze_data": {"freeze_type": "llm_unavailable"},
                        "error": {"recoverable": True},
                    },
                    next=(),
                )
            ),
            aupdate_state=AsyncMock(),
        )
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._graph = graph

        terminal = await agent._arm_worker_batch(
            job_id=str(uuid4()),
            graph_input=None,
            thread_config={"configurable": {"thread_id": "job"}},
            target_wall_seconds=60,
            min_wall_seconds=0,
            iteration_cap=10,
            retry_exhausted=True,
        )

        graph.aupdate_state.assert_not_awaited()
        assert terminal["error"]["type"] == "worker_retry_budget_exhausted"

    @pytest.mark.asyncio
    async def test_explicit_resume_generation_reenters_human_end_through_start(self):
        graph = SimpleNamespace(
            aget_state=AsyncMock(
                return_value=SimpleNamespace(
                    values={
                        "iteration": 8,
                        "should_stop": True,
                        "goal_achieved": False,
                        "freeze_data": {"freeze_type": "phase_boundary"},
                        "error": None,
                    },
                    next=(),
                )
            ),
            aupdate_state=AsyncMock(),
        )
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._graph = graph

        terminal = await agent._arm_worker_batch(
            job_id=str(uuid4()),
            graph_input=None,
            thread_config={"configurable": {"thread_id": "job"}},
            target_wall_seconds=60,
            min_wall_seconds=0,
            iteration_cap=10,
            resume_id="resume-generation-2",
        )

        updates = graph.aupdate_state.await_args.args[1]
        assert graph.aupdate_state.await_args.kwargs == {"as_node": "__start__"}
        assert updates["should_stop"] is False
        assert updates["freeze_data"] is None
        assert updates["worker_resume_id"] == "resume-generation-2"
        assert terminal is None

    @pytest.mark.asyncio
    async def test_applied_resume_generation_does_not_reopen_ambiguous_end(self):
        values = {
            "iteration": 9,
            "should_stop": True,
            "goal_achieved": False,
            "freeze_data": {"freeze_type": "phase_boundary"},
            "error": None,
            "worker_resume_id": "resume-generation-2",
        }
        graph = SimpleNamespace(
            aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=())),
            aupdate_state=AsyncMock(),
        )
        agent = UniversalAgent.__new__(UniversalAgent)
        agent._graph = graph

        terminal = await agent._arm_worker_batch(
            job_id=str(uuid4()),
            graph_input=None,
            thread_config={"configurable": {"thread_id": "job"}},
            target_wall_seconds=60,
            min_wall_seconds=0,
            iteration_cap=10,
            resume_id="resume-generation-2",
        )

        graph.aupdate_state.assert_not_awaited()
        assert terminal == values


def test_worker_environment_is_restored_between_claims(monkeypatch):
    agent = UniversalAgent.__new__(UniversalAgent)
    agent._worker_env_restore = {}
    monkeypatch.setenv("TENANT_ONLY_KEY", "pod-baseline")
    monkeypatch.delenv("PGPASSWORD", raising=False)

    agent._capture_worker_environment(
        {
            "resolved_config": {"agent": {"env_keys": {"TENANT_ONLY_KEY": "secret"}}},
            "datasources": [{"type": "postgresql"}],
        }
    )
    os.environ["TENANT_ONLY_KEY"] = "secret"
    os.environ["PGPASSWORD"] = "database-secret"

    agent._restore_worker_environment()

    assert os.environ["TENANT_ONLY_KEY"] == "pod-baseline"
    assert "PGPASSWORD" not in os.environ
