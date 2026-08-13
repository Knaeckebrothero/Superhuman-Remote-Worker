"""HTTP contracts for the Gate-3 ``/complete`` admission wrapper.

These tests deliberately stub the legacy completion body.  Its side effects
remain covered by the existing endpoint suites; this file proves only the dark
gate, admission ordering, durable outcome handoff, and replay response matrix.
"""

from __future__ import annotations

import builtins
import copy
import inspect
import json
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from starlette.responses import JSONResponse

import main
from services import job_completion_commands as commands
from services import completion as completion_service


JOB_ID = "11111111-2222-3333-4444-555555555555"
AGENT_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
REPORT_ID = UUID("99999999-8888-7777-6666-555555555555")
COMMAND_ID = "12345678-1234-5678-9abc-123456789abc"
CURATOR_ID = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"


class _RecordingRunner:
    """Small in-memory model of the durable effect-runner route contract."""

    def __init__(self) -> None:
        self.command_id = COMMAND_ID
        self.owner = "recording-finalizer-owner"
        self.started: set[str] = set()
        self.details: dict[str, object] = {}
        self.pending: dict[str, object] = {}
        self.intents: dict[str, dict[str, object]] = {}
        self.higher_report_seq = False
        self.probe_order: list[str] = []
        self.order: list[tuple[str, str]] = []
        self.callback_counts: Counter[str] = Counter()
        self.retry_if_names: set[str] = set()
        self.transactional_names: set[str] = set()

    async def run(
        self,
        *,
        name: str,
        group: str,
        callback,
        retry_on_error: bool = False,
        error_output=None,
        retry_if=None,
        depends_on_groups=(),
        effect_timeout_seconds=None,
        command_lease_seconds=None,
    ):
        del depends_on_groups
        self.started.add(name)
        if name in self.details:
            return copy.deepcopy(self.details[name])
        if retry_if is not None:
            self.retry_if_names.add(name)
        if name == "workspace_archive_teardown":
            assert effect_timeout_seconds == 890.0
            assert command_lease_seconds == 900.0
        else:
            assert effect_timeout_seconds is None
            assert command_lease_seconds is None
        self.order.append((name, group))
        self.callback_counts[name] += 1
        try:
            detail = await callback()
        except Exception as exc:
            if not retry_on_error or error_output is None:
                raise
            detail = error_output(exc)
            if inspect.isawaitable(detail):
                detail = await detail
            self.pending[name] = copy.deepcopy(detail)
            return copy.deepcopy(detail)
        if retry_if is not None and retry_if(detail):
            self.pending[name] = copy.deepcopy(detail)
            return copy.deepcopy(detail)
        self.pending.pop(name, None)
        self.details[name] = copy.deepcopy(detail)
        return copy.deepcopy(detail)

    async def run_transactional(self, **kwargs):
        self.transactional_names.add(str(kwargs["name"]))
        return await self.run(**kwargs)

    async def has_started(self, name: str) -> bool:
        return name in self.started

    async def has_completed(self, name: str) -> bool:
        return name in self.details

    async def completed_detail(self, name: str):
        detail = self.details.get(name)
        return copy.deepcopy(detail)

    async def capture_intent(self, name: str, detail=None):
        self.probe_order.append("intent_write" if detail is not None else "intent_read")
        existing = self.intents.get(name)
        if existing is not None:
            if detail is not None and existing != detail:
                raise RuntimeError("completion effect intent identity drifted")
            return copy.deepcopy(existing)
        if detail is None:
            return None
        self.intents[name] = copy.deepcopy(detail)
        return copy.deepcopy(detail)


class _RouteDB:
    """Minimum DB surface for exercising the real legacy route with a runner."""

    def __init__(self, job: dict) -> None:
        self.job = job
        self.execute_count = 0
        self.status_write_count = 0

    async def get_job(self, _job_id: str) -> dict:
        return copy.deepcopy(self.job)

    @asynccontextmanager
    async def acquire(self):
        database = self

        class _Connection:
            async def execute(self, _sql: str, *_args):
                database.execute_count += 1
                return "UPDATE 1"

        yield _Connection()

    async def update_job_status(self, _job_id: str, **updates) -> bool:
        expected = updates.get("expected_status")
        if expected is not None and self.job["status"] != expected:
            return False
        self.status_write_count += 1
        self.job["status"] = updates["status"]
        if updates["status"] == "completed":
            self.job["completed_at"] = "set"
        if "assigned_agent_id" in updates:
            self.job["assigned_agent_id"] = None
        return True


def _route_job(*, status: str = "processing") -> dict:
    return {
        "id": JOB_ID,
        "status": status,
        "execution_lane": "pinned",
        "assigned_agent_id": str(AGENT_ID),
        "freeze_data": None,
        "context": {},
        "parent_job_id": None,
        "project_id": None,
        "user_id": None,
        "config_name": "defaults",
        "config_override": None,
        "resolved_config": {
            "agent": {
                "autonomy": "full",
                "verification": {"enabled": False},
                "curator": {"enabled": False},
            }
        },
        "cloud_diff_baseline_commit": None,
        "merge_status": None,
        "repo_name": None,
        "branch_name": None,
    }


def _body() -> main.JobCompleteRequest:
    return main.JobCompleteRequest(
        should_stop=True,
        goal_achieved=True,
        error=None,
        freeze_data={"freeze_type": "job_complete", "summary": "done"},
        lease_token=17,
        agent_id=AGENT_ID,
        client_report_id=REPORT_ID,
    )


def _accepted(
    disposition: str,
    *,
    state: str = "pending",
    outcome: dict | None = None,
    winning_report_seq: int | None = None,
    abandoned_effects: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        disposition=disposition,
        command_id=COMMAND_ID,
        job_id=JOB_ID,
        report_seq=3,
        state=state,
        stored_payload={},
        outcome=outcome,
        winning_report_seq=winning_report_seq,
        abandoned_effects=abandoned_effects,
        client_report_id=str(REPORT_ID),
        queue_terminalized=False,
    )


def _response_json(response: JSONResponse) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _journaled_entry(
    *,
    resolution: str | None,
    infra_attempts: int = 0,
    memory_retries: int = 0,
    llm_outage: dict[str, object] | None = None,
) -> dict[str, object]:
    """Fixed-cardinality S1 decision snapshot used by crash-resume tests."""

    return {
        "entry_status": "processing",
        "entry_assigned_agent_id": str(AGENT_ID),
        "entry_updated_at": None,
        "matched": False,
        "entry_needs_vm": False,
        "entry_parent_status": None,
        "entry_resolution": resolution,
        "entry_infra_transient_attempts": infra_attempts,
        "entry_memory_retry_count": memory_retries,
        "entry_llm_outage": llm_outage
        or {
            "attempt": 0,
            "first_failed_at": None,
            "last_failed_at": None,
            "next_retry_at": None,
            "fingerprint": None,
            "repeat_key": None,
            "repeats": 0,
            "shape_nudge_attempted": False,
        },
    }


def _forbid_finalizer(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    getter = MagicMock(side_effect=AssertionError("finalizer must not be accessed"))
    monkeypatch.setattr(main, "_get_completion_finalizer", getter)
    return getter


def _patch_normal_route_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    database: _RouteDB,
    terminal_effects: AsyncMock,
    workspace_cleanup: AsyncMock,
) -> None:
    """Isolate the two independently retryable tail groups under test."""

    monkeypatch.setattr(main, "postgres_db", database)
    monkeypatch.setattr(main, "gitea_client", SimpleNamespace(is_initialized=False))
    monkeypatch.setattr(main, "vector_db", None)
    monkeypatch.setattr(main, "_archive_and_cleanup_workspace", workspace_cleanup)
    monkeypatch.setattr(main, "maybe_wake_session", AsyncMock())
    monkeypatch.setattr(main, "_trigger_dispatch", MagicMock())
    monkeypatch.setattr(main, "_kick_session_wake_drain", MagicMock())
    for helper in (
        "_handle_critic_verdict_on_complete",
        "_handle_scholar_completion",
        "_handle_delegation_child_completion",
        "_trigger_verification_on_complete",
        "_advance_project_loop",
    ):
        monkeypatch.setattr(main, helper, AsyncMock())

    from services import completion as completion_service

    monkeypatch.setattr(
        completion_service,
        "apply_deliverable_gate",
        AsyncMock(
            side_effect=lambda _job, _result, status, **_kwargs: (status, [], False)
        ),
    )
    monkeypatch.setattr(
        completion_service,
        "apply_terminal_job_side_effects",
        terminal_effects,
    )


@pytest.mark.asyncio
async def test_effect_runner_replays_early_return_without_reentering_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job(status="completed"))
    runner = _RecordingRunner()
    monkeypatch.setattr(main, "postgres_db", database)

    first = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        _body(),
        _authorized=True,
        _effect_runner=runner,
    )
    replay = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        _body(),
        _authorized=True,
        _effect_runner=runner,
    )

    expected = {
        "status": "handled",
        "job_id": JOB_ID,
        "new_status": "completed",
        "actions": ["late callback ignored; job already completed"],
    }
    assert first == expected
    assert replay == expected
    assert runner.order == [("late_callback_guard", "entry")]
    assert runner.callback_counts == {"late_callback_guard": 1}
    assert await runner.has_completed("late_callback_guard")
    assert await runner.completed_detail("late_callback_guard") == {
        "entry_status": "completed",
        "entry_assigned_agent_id": str(AGENT_ID),
        "entry_updated_at": None,
        "matched": True,
        "entry_needs_vm": False,
        "entry_parent_status": None,
        "entry_resolution": "completed",
        "entry_infra_transient_attempts": 0,
        "entry_memory_retry_count": 0,
        "entry_llm_outage": {
            "attempt": 0,
            "first_failed_at": None,
            "last_failed_at": None,
            "next_retry_at": None,
            "fingerprint": None,
            "repeat_key": None,
            "repeats": 0,
            "shape_nudge_attempted": False,
        },
    }
    assert database.status_write_count == 0


@pytest.mark.asyncio
async def test_effect_runner_reconstructs_normal_result_without_repeating_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job())
    runner = _RecordingRunner()
    terminal_effects = AsyncMock(return_value={"actions": ["terminal durable"]})
    workspace_cleanup = AsyncMock(return_value=["workspace archived"])
    wake = AsyncMock()
    dispatch = MagicMock()
    wake_drain = MagicMock()

    monkeypatch.setattr(main, "postgres_db", database)
    monkeypatch.setattr(main, "gitea_client", SimpleNamespace(is_initialized=False))
    monkeypatch.setattr(main, "vector_db", None)
    monkeypatch.setattr(main, "_archive_and_cleanup_workspace", workspace_cleanup)
    monkeypatch.setattr(main, "maybe_wake_session", wake)
    monkeypatch.setattr(main, "_trigger_dispatch", dispatch)
    monkeypatch.setattr(main, "_kick_session_wake_drain", wake_drain)
    for helper in (
        "_handle_critic_verdict_on_complete",
        "_handle_scholar_completion",
        "_handle_delegation_child_completion",
        "_trigger_verification_on_complete",
        "_advance_project_loop",
    ):
        monkeypatch.setattr(main, helper, AsyncMock())

    from services import completion as completion_service

    monkeypatch.setattr(
        completion_service,
        "apply_deliverable_gate",
        AsyncMock(
            side_effect=lambda _job, _result, status, **_kwargs: (status, [], False)
        ),
    )
    monkeypatch.setattr(
        completion_service,
        "apply_terminal_job_side_effects",
        terminal_effects,
    )

    body = main.JobCompleteRequest(should_stop=True, goal_achieved=True)
    first = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        body,
        _authorized=True,
        _effect_runner=runner,
    )
    replay = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        body,
        _authorized=True,
        _effect_runner=runner,
    )

    expected = {
        "status": "handled",
        "job_id": JOB_ID,
        "new_status": "completed",
        "actions": [
            "status -> completed",
            "terminal durable",
            "workspace archived",
        ],
    }
    assert first == expected
    assert replay == expected
    assert runner.order[0] == ("late_callback_guard", "entry")
    assert runner.order[-1] == ("session_wake_drain_kick", "session_wake_kick")
    assert len(runner.order) == len({name for name, _group in runner.order})
    assert set(runner.callback_counts.values()) == {1}
    assert await runner.has_completed("main_status_write")
    assert "main_status_write" in runner.transactional_names
    assert await runner.completed_detail("workspace_archive_teardown") == {
        "actions": ["workspace archived"]
    }
    assert database.status_write_count == 1
    assert database.job["status"] == "completed"
    terminal_effects.assert_awaited_once()
    workspace_cleanup.assert_awaited_once_with(JOB_ID)
    wake.assert_awaited_once_with(database, JOB_ID, "completed")
    dispatch.assert_called_once_with()
    wake_drain.assert_called_once_with(database)


@pytest.mark.asyncio
async def test_resume_rejects_same_status_without_this_commands_completed_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job(status="completed"))
    runner = _RecordingRunner()
    runner.details["late_callback_guard"] = {
        "entry_status": "processing",
        "entry_assigned_agent_id": str(AGENT_ID),
        "entry_updated_at": None,
        "matched": False,
    }
    # An intent is not ownership proof.  A human/concurrent writer may have
    # reached the same status while this callback was in flight.
    runner.started.add("main_status_write")
    terminal_effects = AsyncMock(return_value={"actions": []})
    workspace_cleanup = AsyncMock(return_value=[])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=workspace_cleanup,
    )

    with pytest.raises(HTTPException) as raised:
        await main._complete_job_legacy(
            MagicMock(),
            JOB_ID,
            _body(),
            _authorized=True,
            _effect_runner=runner,
        )

    assert raised.value.status_code == 409
    assert "out-of-band job control race" in str(raised.value.detail)
    assert database.status_write_count == 0
    terminal_effects.assert_not_awaited()
    workspace_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_pre_status_pause_replays_exact_early_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job(status="paused"))
    runner = _RecordingRunner()
    runner.details["late_callback_guard"] = {
        "entry_status": "processing",
        "entry_assigned_agent_id": str(AGENT_ID),
        "entry_updated_at": None,
        "matched": False,
    }
    expected = {
        "status": "handled",
        "job_id": JOB_ID,
        "new_status": "paused",
        "actions": [
            "infra_transient: paused for retry (attempt 1/5, next retry in 5s, "
            "workspace kept)"
        ],
    }
    runner.details["infra_transient_pause"] = copy.deepcopy(expected)
    terminal_effects = AsyncMock(return_value={"actions": []})
    workspace_cleanup = AsyncMock(return_value=[])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=workspace_cleanup,
    )
    body = main.JobCompleteRequest(
        should_stop=True,
        goal_achieved=False,
        error={"type": "infra_transient", "message": "database unavailable"},
    )

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        body,
        _authorized=True,
        _effect_runner=runner,
    )

    assert result == expected
    assert runner.callback_counts["infra_transient_pause"] == 0
    assert database.status_write_count == 0
    terminal_effects.assert_not_awaited()
    workspace_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_infra_retry_ceiling_replay_uses_journaled_entry_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.completion import INFRA_TRANSIENT_MAX_ATTEMPTS

    job = _route_job(status="paused")
    job["context"] = {"infra_transient": {"attempts": INFRA_TRANSIENT_MAX_ATTEMPTS}}
    database = _RouteDB(job)
    runner = _RecordingRunner()
    runner.details["late_callback_guard"] = _journaled_entry(
        resolution="failed",
        infra_attempts=INFRA_TRANSIENT_MAX_ATTEMPTS - 1,
    )
    expected = {
        "status": "handled",
        "job_id": JOB_ID,
        "new_status": "paused",
        "actions": ["journaled final infra retry"],
    }
    runner.details["infra_transient_pause"] = copy.deepcopy(expected)
    monkeypatch.setattr(main, "postgres_db", database)

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        main.JobCompleteRequest(
            should_stop=True,
            goal_achieved=False,
            error={
                "type": "infra_transient",
                "message": "database unavailable",
            },
        ),
        _authorized=True,
        _effect_runner=runner,
    )

    assert result == expected
    assert runner.callback_counts["infra_transient_pause"] == 0
    assert "infra_transient_give_up" not in runner.started
    assert database.status_write_count == 0


@pytest.mark.asyncio
async def test_memory_retry_ceiling_replay_uses_journaled_entry_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.completion import MEMORY_RETRY_CAP

    job = _route_job(status="paused")
    job["context"] = {"memory_retry_count": MEMORY_RETRY_CAP}
    database = _RouteDB(job)
    runner = _RecordingRunner()
    runner.details["late_callback_guard"] = _journaled_entry(
        resolution="paused",
        memory_retries=MEMORY_RETRY_CAP - 1,
    )
    runner.details["memory_kb_retry_pause"] = {
        "paused": True,
        "retry_count": MEMORY_RETRY_CAP,
    }
    terminal_effects = AsyncMock(return_value={"actions": []})
    workspace_cleanup = AsyncMock(return_value=[])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=workspace_cleanup,
    )

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        main.JobCompleteRequest(
            should_stop=True,
            goal_achieved=False,
            freeze_data={
                "freeze_type": "memory_unavailable",
                "reason": "embedding endpoint unavailable",
            },
        ),
        _authorized=True,
        _effect_runner=runner,
    )

    assert result["new_status"] == "paused"
    assert result["actions"] == [
        "memory_unavailable: re-queued for retry "
        f"(memory_retry_count -> {MEMORY_RETRY_CAP})"
    ]
    assert runner.callback_counts["memory_kb_retry_pause"] == 0
    assert "main_status_write" not in runner.started
    terminal_effects.assert_not_awaited()
    workspace_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_llm_outage_attempt_ceiling_replay_uses_journaled_entry_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services import completion as completion_service

    max_attempts = 3
    monkeypatch.setattr(completion_service, "LLM_OUTAGE_MAX_ATTEMPTS", max_attempts)
    monkeypatch.setattr(completion_service, "LLM_OUTAGE_CEILING_SECONDS", 43_200)
    now = datetime.now(timezone.utc).isoformat()
    advanced_outage = {
        "attempt": max_attempts,
        "first_failed_at": now,
        "last_failed_at": now,
        "next_retry_at": now,
        "fingerprint": None,
        "repeat_key": None,
        "repeats": 0,
        "shape_nudge_attempted": False,
    }
    entry_outage = {**advanced_outage, "attempt": max_attempts - 1}
    job = _route_job(status="paused")
    job["context"] = {"llm_outage": advanced_outage}
    database = _RouteDB(job)
    runner = _RecordingRunner()
    runner.details["late_callback_guard"] = _journaled_entry(
        resolution="paused",
        llm_outage=entry_outage,
    )
    runner.details["llm_outage_retry_pause"] = {
        "paused": True,
        "attempt": max_attempts,
        "delay": 30.0,
        "next_retry_at": now,
    }
    terminal_effects = AsyncMock(return_value={"actions": []})
    workspace_cleanup = AsyncMock(return_value=[])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=workspace_cleanup,
    )

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        main.JobCompleteRequest(
            should_stop=True,
            goal_achieved=False,
            freeze_data={
                "freeze_type": "llm_unavailable",
                "classification": "provider_unavailable",
                "error_summary": "provider unavailable",
                "model": "test-model",
            },
        ),
        _authorized=True,
        _effect_runner=runner,
    )

    assert result["new_status"] == "paused"
    assert result["actions"] == [
        "llm_unavailable: paused for backoff re-dispatch "
        f"(attempt {max_attempts}, next retry in 30s)"
    ]
    assert runner.callback_counts["llm_outage_retry_pause"] == 0
    assert "llm_give_up_operator_alert" not in runner.started
    assert "main_status_write" not in runner.started
    terminal_effects.assert_not_awaited()
    workspace_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_main_status_effect_omits_large_freeze_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job())
    database.merge_job_context = AsyncMock()
    runner = _RecordingRunner()
    terminal_effects = AsyncMock(return_value={"actions": []})
    workspace_cleanup = AsyncMock(return_value=[])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=workspace_cleanup,
    )
    large_value = "x" * 32_000

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        main.JobCompleteRequest(
            should_stop=True,
            goal_achieved=False,
            freeze_data={
                "freeze_type": "batch_boundary",
                "phase_number": 7,
                "opaque_checkpoint": large_value,
            },
        ),
        _authorized=True,
        _effect_runner=runner,
    )

    assert result["new_status"] == "paused"
    status_detail = runner.details["main_status_write"]
    assert status_detail == {
        "new_status": "paused",
        "had_assigned_agent": True,
        "stash_and_clear_freeze": True,
    }
    assert "fd_row" not in status_detail
    assert large_value not in json.dumps(status_detail)
    assert len(json.dumps(status_detail)) < 1024


@pytest.mark.asyncio
async def test_durable_drain_stall_counter_requires_domain_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job())
    database.merge_job_context = AsyncMock(return_value=False)
    runner = _RecordingRunner()
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=AsyncMock(return_value={"actions": []}),
        workspace_cleanup=AsyncMock(return_value=[]),
    )

    with pytest.raises(HTTPException) as raised:
        await main._complete_job_legacy(
            MagicMock(),
            JOB_ID,
            main.JobCompleteRequest(
                should_stop=True,
                goal_achieved=False,
                freeze_data={
                    "freeze_type": "batch_boundary",
                    "phase_number": 7,
                },
            ),
            _authorized=True,
            _effect_runner=runner,
        )

    assert raised.value.status_code == 500
    assert raised.value.detail == "drain-stall counter update did not commit"
    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "drain_stall_counter_alert" not in runner.details
    assert runner.callback_counts["drain_stall_counter_alert"] == 1
    assert "drain_stall_counter_alert" in runner.transactional_names


@pytest.mark.asyncio
async def test_legacy_drain_stall_counter_keeps_best_effort_false_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job())
    database.merge_job_context = AsyncMock(return_value=False)
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=AsyncMock(return_value={"actions": []}),
        workspace_cleanup=AsyncMock(return_value=[]),
    )

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        main.JobCompleteRequest(
            should_stop=True,
            goal_achieved=False,
            freeze_data={
                "freeze_type": "batch_boundary",
                "phase_number": 7,
            },
        ),
        _authorized=True,
    )

    assert result["new_status"] == "paused"
    assert result["actions"] == [
        "status -> paused",
        "cleared agent on paused job (re-dispatchable)",
        "freeze stashed to context (auto-redispatch)",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authority", "expected_delete_calls"),
    [("exact_live", 1), ("exact_absent", 0), ("replacement", 0)],
)
async def test_durable_recovery_delete_uses_exact_pod_authority(
    monkeypatch: pytest.MonkeyPatch,
    authority: str,
    expected_delete_calls: int,
) -> None:
    runtime_uid = "55555555-5555-4555-8555-555555555555"
    job = _route_job()
    job["context"] = {
        "workspace_container": {
            "status": "ready",
            "host": "workspace.example",
            "_runtime_incarnation": runtime_uid,
        }
    }
    database = _RouteDB(job)
    runner = _RecordingRunner()
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=AsyncMock(return_value={"actions": []}),
        workspace_cleanup=AsyncMock(return_value=[]),
    )
    authority_probe = AsyncMock(return_value=authority)
    pod_delete = AsyncMock(return_value=True)
    monkeypatch.setattr(
        main.container_provisioner, "workspace_pod_authority", authority_probe
    )
    monkeypatch.setattr(main.container_provisioner, "delete_workspace", pod_delete)

    async def recovery(_job, job_id, _error, *, delete_workspace, **_kwargs):
        assert await delete_workspace(job_id) is True
        return {
            "status": "handled",
            "job_id": job_id,
            "new_status": "paused",
            "paused": True,
            "actions": ["workspace recovery tested"],
        }

    monkeypatch.setattr(completion_service, "handle_pod_workspace_recovery", recovery)

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        main.JobCompleteRequest(
            should_stop=True,
            goal_achieved=False,
            error={"type": "workspace_unavailable", "message": "sshd gone"},
        ),
        _authorized=True,
        _effect_runner=runner,
    )

    assert result["actions"] == ["workspace recovery tested"]
    authority_probe.assert_awaited_once_with(
        main.WorkspaceOwner.job(JOB_ID),
        expected_runtime_incarnation=runtime_uid,
    )
    assert pod_delete.await_count == expected_delete_calls
    if expected_delete_calls:
        pod_delete.assert_awaited_once_with(
            main.WorkspaceOwner.job(JOB_ID),
            expected_runtime_incarnation=runtime_uid,
            wait_for_exact_absence=True,
        )


@pytest.mark.asyncio
async def test_legacy_recovery_delete_keeps_name_based_best_effort_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _route_job()
    job["context"] = {"workspace_container": {"status": "ready"}}
    database = _RouteDB(job)
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=AsyncMock(return_value={"actions": []}),
        workspace_cleanup=AsyncMock(return_value=[]),
    )
    authority_probe = AsyncMock()
    pod_delete = AsyncMock(return_value=False)
    monkeypatch.setattr(
        main.container_provisioner, "workspace_pod_authority", authority_probe
    )
    monkeypatch.setattr(main.container_provisioner, "delete_workspace", pod_delete)

    async def recovery(_job, job_id, _error, *, delete_workspace, **_kwargs):
        # Legacy recovery historically ignores a best-effort False result.
        assert await delete_workspace(job_id) is False
        return {
            "status": "handled",
            "job_id": job_id,
            "new_status": "paused",
            "actions": ["workspace recovery tested"],
        }

    monkeypatch.setattr(completion_service, "handle_pod_workspace_recovery", recovery)

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        main.JobCompleteRequest(
            should_stop=True,
            goal_achieved=False,
            error={"type": "workspace_unavailable", "message": "sshd gone"},
        ),
        _authorized=True,
    )

    assert result == {
        "status": "handled",
        "job_id": JOB_ID,
        "new_status": "paused",
        "actions": ["workspace recovery tested"],
    }
    authority_probe.assert_not_awaited()
    pod_delete.assert_awaited_once_with(main.WorkspaceOwner.job(JOB_ID))


@pytest.mark.asyncio
async def test_terminal_delivery_failure_does_not_block_teardown_and_replays_only_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job())
    runner = _RecordingRunner()
    terminal_effects = AsyncMock(
        side_effect=[
            RuntimeError("gitea unavailable"),
            {"actions": ["terminal durable"]},
        ]
    )
    workspace_cleanup = AsyncMock(return_value=["workspace archived"])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=workspace_cleanup,
    )
    body = main.JobCompleteRequest(should_stop=True, goal_achieved=True)

    first = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        body,
        _authorized=True,
        _effect_runner=runner,
    )

    assert first["actions"] == ["status -> completed", "workspace archived"]
    assert runner.pending["terminal_merge_change_record"] == {
        "actions": [],
        "error": "gitea unavailable",
    }
    assert "terminal_merge_change_record" not in runner.details
    assert runner.details["workspace_archive_teardown"] == {
        "actions": ["workspace archived"]
    }
    assert {
        "terminal_merge_change_record",
        "workspace_archive_teardown",
    }.issubset(runner.retry_if_names)
    terminal_effects.assert_awaited_once()
    workspace_cleanup.assert_awaited_once_with(JOB_ID)

    replay = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        body,
        _authorized=True,
        _effect_runner=runner,
    )

    assert replay["actions"] == [
        "status -> completed",
        "terminal durable",
        "workspace archived",
    ]
    assert "terminal_merge_change_record" not in runner.pending
    assert runner.details["terminal_merge_change_record"] == {
        "actions": ["terminal durable"]
    }
    assert runner.callback_counts["terminal_merge_change_record"] == 2
    assert runner.callback_counts["workspace_archive_teardown"] == 1
    assert terminal_effects.await_count == 2
    workspace_cleanup.assert_awaited_once_with(JOB_ID)


@pytest.mark.asyncio
async def test_session_wake_failure_does_not_block_independent_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job())
    runner = _RecordingRunner()
    terminal_effects = AsyncMock(return_value={"actions": ["terminal durable"]})
    workspace_cleanup = AsyncMock(return_value=["workspace archived"])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=workspace_cleanup,
    )
    wake = AsyncMock(side_effect=RuntimeError("session wake store unavailable"))
    monkeypatch.setattr(main, "maybe_wake_session", wake)

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        main.JobCompleteRequest(should_stop=True, goal_achieved=True),
        _authorized=True,
        _effect_runner=runner,
    )

    assert result["actions"] == [
        "status -> completed",
        "terminal durable",
        "workspace archived",
    ]
    assert runner.pending["session_wake_enqueue"] == {
        "enqueued": False,
        "error": "session wake store unavailable",
    }
    assert "session_wake_enqueue" not in runner.details
    assert runner.details["workspace_archive_teardown"] == {
        "actions": ["workspace archived"]
    }
    assert runner.order.index(("session_wake_enqueue", "session_wake_enqueue")) < (
        runner.order.index(("workspace_archive_teardown", "workspace_teardown"))
    )
    wake.assert_awaited_once_with(database, JOB_ID, "completed")
    workspace_cleanup.assert_awaited_once_with(JOB_ID)


@pytest.mark.asyncio
async def test_session_wake_retry_policy_is_dark_without_effect_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job())
    terminal_effects = AsyncMock(return_value={"actions": ["terminal durable"]})
    workspace_cleanup = AsyncMock(return_value=["workspace archived"])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=workspace_cleanup,
    )
    failure = RuntimeError("legacy session wake failure")
    wake = AsyncMock(side_effect=failure)
    monkeypatch.setattr(main, "maybe_wake_session", wake)

    with pytest.raises(HTTPException) as caught:
        await main._complete_job_legacy(
            MagicMock(),
            JOB_ID,
            main.JobCompleteRequest(should_stop=True, goal_achieved=True),
            _authorized=True,
            _effect_runner=None,
        )

    assert caught.value.status_code == 500
    assert caught.value.detail == "legacy session wake failure"
    assert caught.value.__cause__ is failure
    wake.assert_awaited_once_with(database, JOB_ID, "completed")
    workspace_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_teardown_failure_stays_pending_and_replay_skips_done_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _RouteDB(_route_job())
    runner = _RecordingRunner()
    terminal_effects = AsyncMock(return_value={"actions": ["terminal durable"]})
    workspace_cleanup = AsyncMock(
        side_effect=[RuntimeError("kubernetes unavailable"), ["workspace archived"]]
    )
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=workspace_cleanup,
    )
    body = main.JobCompleteRequest(should_stop=True, goal_achieved=True)

    first = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        body,
        _authorized=True,
        _effect_runner=runner,
    )

    assert first["actions"] == [
        "status -> completed",
        "terminal durable",
        "workspace cleanup failed: kubernetes unavailable",
    ]
    assert runner.details["terminal_merge_change_record"] == {
        "actions": ["terminal durable"]
    }
    assert runner.pending["workspace_archive_teardown"] == {
        "actions": ["workspace cleanup failed: kubernetes unavailable"],
        "error": "kubernetes unavailable",
    }
    terminal_effects.assert_awaited_once()
    workspace_cleanup.assert_awaited_once_with(JOB_ID)

    replay = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        body,
        _authorized=True,
        _effect_runner=runner,
    )

    assert replay["actions"] == [
        "status -> completed",
        "terminal durable",
        "workspace archived",
    ]
    assert "workspace_archive_teardown" not in runner.pending
    assert runner.details["workspace_archive_teardown"] == {
        "actions": ["workspace archived"]
    }
    assert runner.callback_counts["terminal_merge_change_record"] == 1
    assert runner.callback_counts["workspace_archive_teardown"] == 2
    terminal_effects.assert_awaited_once()
    assert workspace_cleanup.await_count == 2


@pytest.mark.asyncio
async def test_flagged_kubernetes_teardown_captures_and_uses_exact_uids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _route_job()
    job["context"] = {"workspace_container": {"status": "ready", "provisioner": "k8s"}}
    database = _RouteDB(job)
    runner = _RecordingRunner()
    terminal_effects = AsyncMock(return_value={"actions": ["terminal durable"]})
    legacy_cleanup = AsyncMock(return_value=["legacy cleanup"])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=legacy_cleanup,
    )
    host_key = "SHA256:" + ("A" * 43)
    identity = main.WorkspaceTeardownIdentity(
        pod_uid="pod-uid-a",
        pvc_uid="pvc-uid-a",
        service_uid="service-uid-a",
        pod_ip="10.0.0.8",
        ssh_host_key_fingerprint=host_key,
    )

    async def capture(owner):
        assert owner == main.WorkspaceOwner.job(JOB_ID)
        runner.probe_order.append("capture_resource")
        return identity

    async def release(owner, **kwargs):
        assert owner == main.WorkspaceOwner.job(JOB_ID)
        assert kwargs == {
            "teardown_identity": identity,
            "require_snapshot": True,
            "expected_runtime_incarnation": "pod-uid-a",
            "expected_host_key_fingerprint": host_key,
            "strict_terminal_snapshot": True,
            "terminal_snapshot_generation": COMMAND_ID,
            "terminal_snapshot_created_at": runner.intents[
                "workspace_archive_teardown"
            ]["snapshot_created_at"],
            "strict": True,
            "exact_absence_timeout_seconds": 45.0,
        }
        runner.probe_order.append("release")
        return True

    capture_mock = AsyncMock(side_effect=capture)
    release_mock = AsyncMock(side_effect=release)
    monkeypatch.setattr(
        main.container_provisioner,
        "capture_terminal_workspace_identity",
        capture_mock,
    )
    monkeypatch.setattr(
        main.container_provisioner,
        "release_workspace",
        release_mock,
    )

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        _body(),
        _authorized=True,
        _effect_runner=runner,
    )

    assert "k8s workspace released" in result["actions"]
    assert runner.intents["workspace_archive_teardown"] == {
        "kind": "kubernetes",
        "pod_uid": "pod-uid-a",
        "pvc_uid": "pvc-uid-a",
        "service_uid": "service-uid-a",
        "pod_ip": "10.0.0.8",
        "ssh_host_key_fingerprint": host_key,
        "ssh_port": 30022,
        "snapshot_generation": COMMAND_ID,
        "snapshot_created_at": runner.intents["workspace_archive_teardown"][
            "snapshot_created_at"
        ],
    }
    assert runner.probe_order == [
        "intent_read",
        "capture_resource",
        "intent_write",
        "release",
    ]
    assert runner.callback_counts["workspace_archive_teardown"] == 1
    assert "workspace_archive_teardown" not in runner.pending
    legacy_cleanup.assert_not_awaited()


@pytest.mark.asyncio
async def test_kubernetes_teardown_resume_reuses_intent_after_pod_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _route_job()
    job["context"] = {"workspace_container": {"status": "ready", "provisioner": "k8s"}}
    database = _RouteDB(job)
    runner = _RecordingRunner()
    host_key = "SHA256:" + ("A" * 43)
    snapshot_created_at = "2026-08-13T01:02:03+00:00"
    runner.intents["workspace_archive_teardown"] = {
        "kind": "kubernetes",
        "pod_uid": "old-pod-uid",
        "pvc_uid": "old-pvc-uid",
        "service_uid": "old-service-uid",
        "pod_ip": "10.0.0.9",
        "ssh_host_key_fingerprint": host_key,
        "ssh_port": 30022,
        "snapshot_generation": COMMAND_ID,
        "snapshot_created_at": snapshot_created_at,
    }
    terminal_effects = AsyncMock(return_value={"actions": ["terminal durable"]})
    legacy_cleanup = AsyncMock(return_value=["legacy cleanup"])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=legacy_cleanup,
    )
    capture_mock = AsyncMock(
        side_effect=AssertionError("a captured teardown must not recapture by name")
    )
    release_mock = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(
        main.container_provisioner,
        "capture_terminal_workspace_identity",
        capture_mock,
    )
    monkeypatch.setattr(
        main.container_provisioner,
        "release_workspace",
        release_mock,
    )

    first = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        _body(),
        _authorized=True,
        _effect_runner=runner,
    )
    replay = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        _body(),
        _authorized=True,
        _effect_runner=runner,
    )

    assert first["actions"][-1].startswith("workspace cleanup failed:")
    assert replay["actions"][-1] == "k8s workspace released"
    assert release_mock.await_count == 2
    expected_identity = main.WorkspaceTeardownIdentity(
        pod_uid="old-pod-uid",
        pvc_uid="old-pvc-uid",
        service_uid="old-service-uid",
        pod_ip="10.0.0.9",
        ssh_host_key_fingerprint=host_key,
    )
    assert all(
        call.kwargs
        == {
            "teardown_identity": expected_identity,
            "require_snapshot": True,
            "expected_runtime_incarnation": "old-pod-uid",
            "expected_host_key_fingerprint": host_key,
            "strict_terminal_snapshot": True,
            "terminal_snapshot_generation": COMMAND_ID,
            "terminal_snapshot_created_at": snapshot_created_at,
            "strict": True,
            "exact_absence_timeout_seconds": 45.0,
        }
        for call in release_mock.await_args_list
    )
    capture_mock.assert_not_awaited()
    legacy_cleanup.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_runner", [False, True])
async def test_kubernetes_uid_teardown_stays_off_for_default_off_and_vm_lanes(
    monkeypatch: pytest.MonkeyPatch,
    use_runner: bool,
) -> None:
    job = _route_job()
    job["context"] = {
        "workspace_container": {"status": "ready", "provisioner": "k8s"},
        **({"vm": {"status": "ready"}} if use_runner else {}),
    }
    database = _RouteDB(job)
    terminal_effects = AsyncMock(return_value={"actions": ["terminal durable"]})
    legacy_cleanup = AsyncMock(return_value=["legacy cleanup"])
    _patch_normal_route_dependencies(
        monkeypatch,
        database=database,
        terminal_effects=terminal_effects,
        workspace_cleanup=legacy_cleanup,
    )
    capture_mock = AsyncMock()
    release_mock = AsyncMock()
    monkeypatch.setattr(
        main.container_provisioner,
        "capture_workspace_teardown_identity",
        capture_mock,
    )
    monkeypatch.setattr(
        main.container_provisioner,
        "release_workspace",
        release_mock,
    )

    result = await main._complete_job_legacy(
        MagicMock(),
        JOB_ID,
        _body(),
        _authorized=True,
        _effect_runner=_RecordingRunner() if use_runner else None,
    )

    assert result["actions"][-1] == "legacy cleanup"
    legacy_cleanup.assert_awaited_once_with(JOB_ID)
    capture_mock.assert_not_awaited()
    release_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_off_bypasses_command_module_and_preserves_legacy_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The closed gate must be byte-for-byte legacy at the wrapper boundary."""

    request = MagicMock()
    body = _body()
    legacy_result = {
        "status": "success",
        "job_id": JOB_ID,
        "new_status": "completed",
        "actions": ["legacy action"],
    }
    legacy = AsyncMock(return_value=legacy_result)
    accept = AsyncMock(side_effect=AssertionError("accept must remain dark"))
    auth = AsyncMock()
    finalizer_getter = _forbid_finalizer(monkeypatch)

    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", False)
    monkeypatch.setattr(main, "require_internal", auth)
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)

    original_import = builtins.__import__

    def reject_command_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in {
            "services.job_completion_commands",
            "services.completion_finalizer",
        }:
            raise AssertionError("closed gate attempted completion-service import")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_command_import)

    handled = await main.complete_job(request, JOB_ID, body)

    assert handled is legacy_result
    auth.assert_awaited_once_with(request)
    legacy.assert_awaited_once_with(request, JOB_ID, body, _authorized=True)
    accept.assert_not_awaited()
    finalizer_getter.assert_not_called()


@pytest.mark.asyncio
async def test_accepted_stateless_command_does_not_recheck_terminalized_worker_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept owns the worker fence; legacy mode still checks the live lease."""

    from src.shared import worker_queue

    job = _route_job(status="completed")
    job["execution_lane"] = "stateless"
    database = _RouteDB(job)
    runner = _RecordingRunner()
    stale_lease = AsyncMock(return_value=False)
    accept = AsyncMock(return_value=_accepted("fresh"))

    async def finalize(command_id, *, callback, inline):
        assert command_id == COMMAND_ID
        assert inline is True
        outcome = await callback(runner)
        return SimpleNamespace(
            disposition="done",
            state="done",
            outcome=outcome,
        )

    finalizer = SimpleNamespace(finalize_command=AsyncMock(side_effect=finalize))
    monkeypatch.setattr(main, "postgres_db", database)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(
        main, "_get_completion_finalizer", MagicMock(return_value=finalizer)
    )
    monkeypatch.setattr(commands, "accept_completion_command", accept)
    monkeypatch.setattr(worker_queue, "worker_lease_is_current", stale_lease)

    # Command admission already checked token 17 and terminalized that queue
    # unit. The durable workflow must use its immutable accepted fence instead
    # of requiring a lease that can no longer be live after accept.
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    handled = await main.complete_job(MagicMock(), JOB_ID, _body())

    assert handled == {
        "status": "handled",
        "job_id": JOB_ID,
        "new_status": "completed",
        "actions": ["late callback ignored; job already completed"],
    }
    stale_lease.assert_not_awaited()
    accept.assert_awaited_once()

    # With the gate closed there is no accepted command fence, so the same
    # stale token must still fail the historical thin entry check.
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", False)
    with pytest.raises(HTTPException) as rejected:
        await main.complete_job(MagicMock(), JOB_ID, _body())

    assert rejected.value.status_code == 409
    assert rejected.value.detail == (
        "Completion report does not hold the current worker lease"
    )
    stale_lease.assert_awaited_once()
    assert stale_lease.await_args.kwargs == {
        "job_id": JOB_ID,
        "lease_token": 17,
    }
    assert accept.await_count == 1


@pytest.mark.asyncio
async def test_fresh_admission_precedes_inline_finalization_and_returns_exact_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = MagicMock()
    body = _body()
    database = object()
    runner = _RecordingRunner()
    events: list[str] = []
    legacy_result = {
        "status": "success",
        "job_id": JOB_ID,
        "new_status": "completed",
        "actions": ["effect-a", "effect-b"],
    }

    async def auth(_request) -> None:
        events.append("authenticate")

    async def accept(*args, **kwargs):
        events.append("accept")
        return _accepted("fresh")

    async def legacy(*args, **kwargs):
        events.append("legacy")
        return legacy_result

    async def finalize(command_id, *, callback, inline):
        events.append("finalize")
        assert command_id == COMMAND_ID
        assert inline is True
        outcome = await callback(runner)
        events.append("terminal")
        return SimpleNamespace(
            disposition="done",
            state="done",
            outcome=outcome,
        )

    accept_mock = AsyncMock(side_effect=accept)
    legacy_mock = AsyncMock(side_effect=legacy)
    finalize_mock = AsyncMock(side_effect=finalize)
    finalizer = SimpleNamespace(finalize_command=finalize_mock)
    finalizer_getter = MagicMock(return_value=finalizer)
    delay = AsyncMock()
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "COMPLETION_FINALIZER_INLINE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(main.asyncio, "sleep", delay)
    monkeypatch.setattr(main, "postgres_db", database)
    monkeypatch.setattr(main, "require_internal", AsyncMock(side_effect=auth))
    monkeypatch.setattr(main, "_complete_job_legacy", legacy_mock)
    monkeypatch.setattr(main, "_get_completion_finalizer", finalizer_getter)
    monkeypatch.setattr(commands, "accept_completion_command", accept_mock)

    handled = await main.complete_job(request, JOB_ID, body)

    assert handled is legacy_result
    assert events == ["authenticate", "accept", "finalize", "legacy", "terminal"]
    legacy_mock.assert_awaited_once_with(
        request,
        JOB_ID,
        body,
        _authorized=True,
        _effect_runner=runner,
    )
    finalizer_getter.assert_called_once_with()
    delay.assert_not_awaited()
    finalize_mock.assert_awaited_once()
    accept_mock.assert_awaited_once_with(
        database,
        job_id=JOB_ID,
        payload={
            "should_stop": True,
            "goal_achieved": True,
            "error": None,
            "freeze_data": {
                "freeze_type": "job_complete",
                "summary": "done",
            },
        },
        lease_token=17,
        agent_id=str(AGENT_ID),
        client_report_id=str(REPORT_ID),
        requested_by=f"agent:{AGENT_ID}",
    )


@pytest.mark.asyncio
async def test_fresh_admission_exposes_local_force_delete_window(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []

    async def accept(*_args, **_kwargs):
        events.append("accept")
        return _accepted("fresh")

    async def delay(seconds: float) -> None:
        events.append(f"delay:{seconds}")

    async def finalize(command_id, *, callback, inline):
        assert command_id == COMMAND_ID
        assert inline is True
        events.append("finalize")
        outcome = await callback(_RecordingRunner())
        return SimpleNamespace(
            disposition="done",
            state="done",
            outcome=outcome,
        )

    async def legacy(*_args, **_kwargs):
        events.append("legacy")
        return {"status": "success", "job_id": JOB_ID}

    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "COMPLETION_FINALIZER_INLINE_DELAY_SECONDS", 15.0)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", AsyncMock(side_effect=legacy))
    monkeypatch.setattr(
        commands, "accept_completion_command", AsyncMock(side_effect=accept)
    )
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock(side_effect=delay))
    monkeypatch.setattr(
        main,
        "_get_completion_finalizer",
        MagicMock(
            return_value=SimpleNamespace(
                finalize_command=AsyncMock(side_effect=finalize)
            )
        ),
    )

    with caplog.at_level("INFO"):
        handled = await main.complete_job(MagicMock(), JOB_ID, _body())

    assert handled == {"status": "success", "job_id": JOB_ID}
    assert events == ["accept", "finalize", "delay:15.0", "legacy"]
    accepted_records = [
        record.getMessage()
        for record in caplog.records
        if "Completion command" in record.getMessage()
    ]
    assert accepted_records == [
        f"Completion command {COMMAND_ID} accepted for job {JOB_ID}",
        f"Completion command {COMMAND_ID} claimed for job {JOB_ID}; "
        "inline delay 15.000s",
    ]
    assert all(str(AGENT_ID) not in record for record in accepted_records)
    assert all("lease" not in record.lower() for record in accepted_records)


@pytest.mark.asyncio
async def test_deterministic_http_guard_is_terminal_and_replays_exactly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = MagicMock()
    body = _body()
    runner = _RecordingRunner()
    stored: dict[str, dict] = {}

    async def accept(*args, **kwargs):
        if "outcome" not in stored:
            return _accepted("fresh")
        return _accepted("replay_done", state="done", outcome=stored["outcome"])

    async def finalize(command_id, *, callback, inline):
        assert command_id == COMMAND_ID
        assert inline is True
        stored["outcome"] = await callback(runner)
        return SimpleNamespace(
            disposition="done",
            state="done",
            outcome=stored["outcome"],
        )

    error = HTTPException(
        status_code=422,
        detail={"reason": "deterministic completion guard"},
        headers={"X-Completion-Guard": "true"},
    )
    legacy = AsyncMock(side_effect=error)
    finalize_mock = AsyncMock(side_effect=finalize)
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(
        commands, "accept_completion_command", AsyncMock(side_effect=accept)
    )
    monkeypatch.setattr(
        main,
        "_get_completion_finalizer",
        MagicMock(return_value=SimpleNamespace(finalize_command=finalize_mock)),
    )

    for _attempt in range(2):
        with pytest.raises(HTTPException) as caught:
            await main.complete_job(request, JOB_ID, body)
        assert caught.value.status_code == 422
        assert caught.value.detail == {"reason": "deterministic completion guard"}
        assert caught.value.headers == {"X-Completion-Guard": "true"}

    assert stored["outcome"] == {
        "_completion_http_error": {
            "status_code": 422,
            "detail": {"reason": "deterministic completion guard"},
            "headers": {"X-Completion-Guard": "true"},
        }
    }
    legacy.assert_awaited_once_with(
        request,
        JOB_ID,
        body,
        _authorized=True,
        _effect_runner=runner,
    )
    finalize_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_done_replay_returns_stored_outcome_with_idempotency_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = {
        "status": "success",
        "job_id": JOB_ID,
        "new_status": "completed",
        "actions": ["already finalized"],
    }
    accept = AsyncMock(
        return_value=_accepted("replay_done", state="done", outcome=outcome)
    )
    legacy = AsyncMock()
    finalizer_getter = _forbid_finalizer(monkeypatch)
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)

    response = await main.complete_job(MagicMock(), JOB_ID, _body())

    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert response.headers["Idempotent-Replayed"] == "true"
    assert _response_json(response) == outcome
    legacy.assert_not_awaited()
    finalizer_getter.assert_not_called()


@pytest.mark.asyncio
async def test_pending_replay_is_retryable_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accept = AsyncMock(side_effect=commands.CompletionInProgress(COMMAND_ID, "pending"))
    legacy = AsyncMock()
    finalizer_getter = _forbid_finalizer(monkeypatch)
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)

    with pytest.raises(HTTPException) as caught:
        await main.complete_job(MagicMock(), JOB_ID, _body())

    assert caught.value.status_code == 409
    assert caught.value.headers == {"Retry-After": "1"}
    assert "pending" in str(caught.value.detail)
    legacy.assert_not_awaited()
    finalizer_getter.assert_not_called()


@pytest.mark.asyncio
async def test_divergent_replay_is_unprocessable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accept = AsyncMock(
        side_effect=commands.CompletionPayloadMismatch(
            "client_report_id was reused with a different payload"
        )
    )
    legacy = AsyncMock()
    finalizer_getter = _forbid_finalizer(monkeypatch)
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)

    with pytest.raises(HTTPException) as caught:
        await main.complete_job(MagicMock(), JOB_ID, _body())

    assert caught.value.status_code == 422
    assert caught.value.headers is None
    assert "different payload" in str(caught.value.detail)
    legacy.assert_not_awaited()
    finalizer_getter.assert_not_called()


@pytest.mark.asyncio
async def test_parked_replay_is_accepted_without_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accept = AsyncMock(return_value=_accepted("replay_parked", state="parked"))
    legacy = AsyncMock()
    finalizer_getter = _forbid_finalizer(monkeypatch)
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)

    response = await main.complete_job(MagicMock(), JOB_ID, _body())

    assert isinstance(response, JSONResponse)
    assert response.status_code == 202
    assert "Retry-After" not in response.headers
    assert response.headers["Idempotent-Replayed"] == "true"
    assert _response_json(response) == {
        "status": "still_pending",
        "job_id": JOB_ID,
        "command_id": COMMAND_ID,
        "command_state": "parked",
    }
    legacy.assert_not_awaited()
    finalizer_getter.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "state", "outcome", "winning_seq", "abandoned"),
    [
        (
            "replay_superseded",
            "superseded",
            {
                "status": "superseded",
                "job_id": JOB_ID,
                "winning_report_seq": 2,
            },
            2,
            (),
        ),
        (
            "replay_force_resolved",
            "force_resolved",
            {
                "status": "force_resolved",
                "job_id": JOB_ID,
                "abandoned_effects": ["workspace_cleanup"],
            },
            None,
            ("workspace_cleanup",),
        ),
    ],
)
async def test_operator_terminal_replays_return_their_durable_outcome(
    monkeypatch: pytest.MonkeyPatch,
    disposition: str,
    state: str,
    outcome: dict,
    winning_seq: int | None,
    abandoned: tuple[str, ...],
) -> None:
    accept = AsyncMock(
        return_value=_accepted(
            disposition,
            state=state,
            outcome=outcome,
            winning_report_seq=winning_seq,
            abandoned_effects=abandoned,
        )
    )
    legacy = AsyncMock()
    finalizer_getter = _forbid_finalizer(monkeypatch)
    monkeypatch.setattr(main, "COMPLETION_COMMANDS_ENABLED", True)
    monkeypatch.setattr(main, "require_internal", AsyncMock())
    monkeypatch.setattr(main, "_complete_job_legacy", legacy)
    monkeypatch.setattr(commands, "accept_completion_command", accept)

    response = await main.complete_job(MagicMock(), JOB_ID, _body())

    assert isinstance(response, JSONResponse)
    assert response.status_code == 200
    assert response.headers["Idempotent-Replayed"] == "true"
    assert _response_json(response) == outcome
    assert "Retry-After" not in response.headers
    legacy.assert_not_awaited()
    finalizer_getter.assert_not_called()


@pytest.mark.asyncio
async def test_curation_handoff_is_keyed_to_completion_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        async def fetchrow(self, *_args, **_kwargs):
            return {"id": CURATOR_ID, "status": "waiting"}

    @asynccontextmanager
    async def acquire():
        yield _Conn()

    queue = AsyncMock(return_value=True)
    monkeypatch.setattr(completion_service, "is_curation_enabled", lambda _job: True)
    monkeypatch.setattr(
        completion_service,
        "get_curation_config",
        lambda _job: {"curator_config": "curator"},
    )
    monkeypatch.setattr(main.postgres_db, "acquire", acquire)
    monkeypatch.setattr(
        main.postgres_db,
        "get_job",
        AsyncMock(return_value={"id": CURATOR_ID, "context": {}}),
    )
    monkeypatch.setattr(main, "_internal_resume_job", queue)

    await main._trigger_curation_final_pass(
        JOB_ID,
        {"id": JOB_ID, "resolved_config": {}},
        completion_command_id=COMMAND_ID,
    )

    assert queue.await_args.kwargs["additional_context"] == {
        "curation_final_pass_completion_command_id": COMMAND_ID
    }


@pytest.mark.asyncio
async def test_curation_handoff_reconciles_exact_command_without_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        async def fetchrow(self, *_args, **_kwargs):
            return {"id": CURATOR_ID, "status": "paused"}

    @asynccontextmanager
    async def acquire():
        yield _Conn()

    queue = AsyncMock()
    dispatch = MagicMock()
    monkeypatch.setattr(completion_service, "is_curation_enabled", lambda _job: True)
    monkeypatch.setattr(
        completion_service,
        "get_curation_config",
        lambda _job: {"curator_config": "curator"},
    )
    monkeypatch.setattr(main.postgres_db, "acquire", acquire)
    monkeypatch.setattr(
        main.postgres_db,
        "get_job",
        AsyncMock(
            return_value={
                "id": CURATOR_ID,
                "context": {"curation_final_pass_completion_command_id": COMMAND_ID},
            }
        ),
    )
    monkeypatch.setattr(main, "_internal_resume_job", queue)
    monkeypatch.setattr(main, "_trigger_dispatch", dispatch)

    await main._trigger_curation_final_pass(
        JOB_ID,
        {"id": JOB_ID, "resolved_config": {}},
        completion_command_id=COMMAND_ID,
    )

    queue.assert_not_awaited()
    dispatch.assert_called_once_with()
