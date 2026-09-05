"""Journal-before-observe for the job_complete decision.

Pins the durability chain of knowledge-base/knowledge/issues/
job_finalization_decisions_held_only_in_process_memory.md:

1. the tool journals the decision through the orchestrator BEFORE it returns
   (and before the process cache is populated),
2. the audited tool node mirrors the journaled decision into checkpointed
   state, so any checkpoint carrying the tool result also carries the decision,
3. finalize_job reads durable-first (cache → checkpointed mirror) and FAILS
   LOUDLY instead of fabricating a placeholder report,
4. the orchestrator impl is idempotent on (job_id, tool_call_id) and refuses
   terminal jobs.

Real-Postgres coverage of the row write + the feedback-resume void lives in
tests/test_queue_job_for_resume.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.api.orchestrator_client import CompletionDecisionError
from agent.tools.core.job import (
    _final_phase_data,
    create_job_tools,
    get_final_phase_data,
    seed_final_phase_data,
)
from tests._tool_invoke import invoke_tool

JOB_ID = "job-journal-test"


@pytest.fixture(autouse=True)
def _clean_caches():
    _final_phase_data.clear()
    yield
    _final_phase_data.clear()


def _context(client) -> MagicMock:
    context = MagicMock()
    context.job_id = JOB_ID
    context.has_workspace.return_value = True
    context.workspace_manager = MagicMock()
    context.has_todo.return_value = False
    context.orchestrator_client = client
    context.subagent_runtime = None
    return context


def _job_complete(client):
    _, job_complete = create_job_tools(_context(client))
    return job_complete


class TestJobCompleteJournal:
    @pytest.mark.asyncio
    async def test_live_or_undelivered_child_blocks_before_journal(self):
        client = MagicMock()
        client.record_completion_decision = AsyncMock()
        context = _context(client)
        context.subagent_runtime = MagicMock()
        context.subagent_runtime.has_completion_blockers.return_value = True
        _, job_complete = create_job_tools(context)

        result = await invoke_tool(
            job_complete,
            {"summary": "Done.", "deliverables": [], "confidence": 1.0},
            call_id="call-too-early",
        )

        assert "blocked while a background subagent" in result
        assert "Reports push automatically" in result
        client.record_completion_decision.assert_not_awaited()
        assert get_final_phase_data(JOB_ID) is None

    @pytest.mark.asyncio
    async def test_child_blocker_probe_failure_fails_closed(self):
        client = MagicMock()
        client.record_completion_decision = AsyncMock()
        context = _context(client)
        context.subagent_runtime = MagicMock()
        context.subagent_runtime.has_completion_blockers.side_effect = RuntimeError(
            "runtime unavailable"
        )
        _, job_complete = create_job_tools(context)

        result = await invoke_tool(
            job_complete,
            {"summary": "Done.", "deliverables": [], "confidence": 1.0},
        )

        assert "could not be verified" in result
        assert "NOT marked as final" in result
        client.record_completion_decision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_journal_write_happens_before_cache(self):
        """The decision must be durable before anything local observes it."""
        cache_at_call_time = {}

        async def _record(**kwargs):
            cache_at_call_time["entry"] = get_final_phase_data(JOB_ID)
            return {"recorded": True, "replay": False}

        client = MagicMock()
        client.record_completion_decision = AsyncMock(side_effect=_record)

        result = await invoke_tool(
            _job_complete(client),
            {"summary": "Done.", "deliverables": [], "confidence": 0.9},
            call_id="call-order-1",
        )

        assert "Phase marked as final" in result
        assert cache_at_call_time["entry"] is None, (
            "the process cache was populated BEFORE the durable write — "
            "a crash in between would observe a decision that was never "
            "journaled"
        )
        assert get_final_phase_data(JOB_ID)["tool_call_id"] == "call-order-1"

    @pytest.mark.asyncio
    async def test_injected_tool_call_id_reaches_the_journal(self):
        client = MagicMock()
        client.record_completion_decision = AsyncMock(
            return_value={"recorded": True, "replay": False}
        )

        await invoke_tool(
            _job_complete(client),
            {"summary": "Done.", "deliverables": [], "confidence": 1.0},
            call_id="call-abc-123",
        )

        kwargs = client.record_completion_decision.await_args.kwargs
        assert kwargs["job_id"] == JOB_ID
        assert kwargs["tool_call_id"] == "call-abc-123"
        assert kwargs["summary"] == "Done."

    @pytest.mark.asyncio
    async def test_journal_failure_reports_not_recorded_and_caches_nothing(self):
        client = MagicMock()
        client.record_completion_decision = AsyncMock(
            side_effect=CompletionDecisionError("HTTP 503: unavailable")
        )

        result = await invoke_tool(
            _job_complete(client),
            {"summary": "Done.", "deliverables": [], "confidence": 0.9},
        )

        assert "NOT be durably recorded" in result
        assert "NOT marked as final" in result
        assert get_final_phase_data(JOB_ID) is None, (
            "a failed journal write must not leave a local decision behind — "
            "that is the dual-write gap this design removes"
        )

    @pytest.mark.asyncio
    async def test_replay_response_is_success(self):
        client = MagicMock()
        client.record_completion_decision = AsyncMock(
            return_value={"recorded": True, "replay": True}
        )

        result = await invoke_tool(
            _job_complete(client),
            {"summary": "Done.", "deliverables": [], "confidence": 0.9},
        )

        assert "Phase marked as final" in result
        assert get_final_phase_data(JOB_ID) is not None

    @pytest.mark.asyncio
    async def test_no_client_degrades_to_cache_only(self):
        result = await invoke_tool(
            _job_complete(None),
            {"summary": "Done.", "deliverables": [], "confidence": 0.9},
        )

        assert "Phase marked as final" in result
        assert get_final_phase_data(JOB_ID) is not None

    @pytest.mark.asyncio
    async def test_seed_final_phase_data_rehydrates_cache(self):
        seed_final_phase_data(
            JOB_ID, {"summary": "S", "deliverables": [], "tool_call_id": "t1"}
        )
        assert get_final_phase_data(JOB_ID)["summary"] == "S"


class TestDecisionStateMirror:
    def test_empty_when_no_decision(self):
        from agent.graph import _decision_state_mirror

        assert _decision_state_mirror(JOB_ID) == {}

    def test_mirrors_completion_decision_and_sets_flag(self):
        from agent.graph import _decision_state_mirror

        _final_phase_data[JOB_ID] = {"summary": "S", "tool_call_id": "t1"}
        updates = _decision_state_mirror(JOB_ID)
        assert updates["is_final_phase"] is True
        assert updates["completion_decision"]["tool_call_id"] == "t1"
        assert "verdict_decision" not in updates

    def test_mirrors_verdict_decision(self):
        from agent.graph import _decision_state_mirror
        from agent.tools.evaluation.evaluation_tools import _verdict_data

        _verdict_data[JOB_ID] = {"_verdict": "returned", "_target_job_id": "t"}
        try:
            updates = _decision_state_mirror(JOB_ID)
            assert updates["is_final_phase"] is True
            assert updates["verdict_decision"]["_verdict"] == "returned"
        finally:
            _verdict_data.clear()


def _finalize_fixtures():
    workspace = MagicMock()
    workspace.git_manager = None
    workspace.get_head_commit.return_value = None
    workspace.get_content_tree.return_value = None
    todo_manager = MagicMock()
    return workspace, todo_manager


class TestFinalizeDurableFirst:
    def test_state_channel_fallback_when_cache_empty(self):
        """A restarted process finalizes from the checkpointed mirror."""
        from agent.core.phase import finalize_job

        workspace, todo_manager = _finalize_fixtures()
        state = {
            "job_id": JOB_ID,
            "metadata": {},
            "phase_number": 5,
            "is_final_phase": True,
            "completion_decision": {
                "summary": "Recovered summary",
                "deliverables": ["output/report.md"],
                "confidence": 0.8,
                "tool_call_id": "call-1",
            },
        }

        result = finalize_job(state, workspace, todo_manager, config=None)

        assert result.success is True
        assert result.freeze_data["freeze_type"] == "job_complete"
        assert result.freeze_data["summary"] == "Recovered summary"
        assert result.freeze_data["deliverables"] == ["output/report.md"]
        assert result.freeze_data["confidence"] == 0.8

    def test_worker_without_decision_rejects_instead_of_fabricating(self):
        """The placeholder report ('Job completed', [], 1.0) must be dead."""
        from agent.core.phase import finalize_job

        workspace, todo_manager = _finalize_fixtures()
        state = {
            "job_id": JOB_ID,
            "metadata": {},
            "phase_number": 5,
            "is_final_phase": True,
        }

        result = finalize_job(state, workspace, todo_manager, config=None)

        assert result.success is False
        assert result.freeze_data is None
        assert "job_complete" in (result.error_message or "")
        workspace.write_file.assert_not_called()

    def test_critic_without_verdict_completes_with_honest_report(self):
        """Fail-closed escalation path keeps working, without fabrication."""
        from agent.core.phase import finalize_job

        workspace, todo_manager = _finalize_fixtures()
        state = {
            "job_id": JOB_ID,
            "metadata": {"verification_target": "target-1"},
            "phase_number": 3,
            "is_final_phase": True,
        }

        result = finalize_job(state, workspace, todo_manager, config=None)

        assert result.success is True
        assert "verdict" not in result.freeze_data, (
            "a missing verdict must never synthesize one — the orchestrator "
            "escalates from the ledger"
        )
        assert "without a durably recorded verdict" in result.freeze_data["summary"]
        assert result.freeze_data["confidence"] == 0.0

    def test_verdict_recovered_from_state_channel(self):
        """A restarted critic's freeze carries the journaled verdict."""
        from agent.core.phase import finalize_job

        workspace, todo_manager = _finalize_fixtures()
        state = {
            "job_id": JOB_ID,
            "metadata": {"verification_target": "target-1"},
            "phase_number": 3,
            "verdict_decision": {
                "_verdict": "returned",
                "_target_job_id": "target-1",
                "round": 2,
                "open_findings": [{"id": "F1"}],
            },
        }

        result = finalize_job(state, workspace, todo_manager, config=None)

        assert result.success is True
        assert result.freeze_data.get("verdict") == "returned"
        assert result.freeze_data.get("target_job_id") == "target-1"

    def test_trigger_fires_from_state_mirror_alone(self):
        """on_strategic_phase_complete finalizes off the checkpointed mirror."""
        from agent.core.phase import on_strategic_phase_complete

        workspace, todo_manager = _finalize_fixtures()
        state = {
            "job_id": JOB_ID,
            "metadata": {},
            "phase_number": 5,
            "is_final_phase": False,  # LWW flag lost; mirror alone must carry
            "completion_decision": {
                "summary": "From mirror",
                "deliverables": [],
                "confidence": 1.0,
            },
        }

        result = on_strategic_phase_complete(state, workspace, todo_manager)

        assert result.success is True
        assert result.freeze_data is not None
        assert result.freeze_data["summary"] == "From mirror"


class TestRecordCompletionDecisionImpl:
    """Orchestrator-side impl contract (mocked DB; row write is pinned on
    real Postgres in tests/test_queue_job_for_resume.py)."""

    def _db(self, job):
        db = MagicMock()
        db.get_job = AsyncMock(return_value=job)
        db.set_completion_decision = AsyncMock(return_value=True)
        return db

    async def _call(self, db, **overrides):
        from orchestrator.main import _record_completion_decision_impl

        kwargs = dict(
            postgres_db=db,
            job_id="j1",
            tool_call_id="call-1",
            summary="Done.",
            deliverables=["output/a.md"],
            confidence=0.9,
            notes=None,
        )
        kwargs.update(overrides)
        return await _record_completion_decision_impl(**kwargs)

    @pytest.mark.asyncio
    async def test_missing_tool_call_id_is_400(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await self._call(
                self._db({"id": "j1", "status": "processing"}), tool_call_id=""
            )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_job_is_404(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await self._call(self._db(None))
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_terminal_job_is_409(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await self._call(self._db({"id": "j1", "status": "completed"}))
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_replay_same_tool_call_id_short_circuits(self):
        stored = {"tool_call_id": "call-1", "summary": "Original."}
        db = self._db(
            {
                "id": "j1",
                "status": "processing",
                # JSONB arrives as a string on the app pool — the impl must
                # parse defensively.
                "context": '{"completion_decision": '
                '{"tool_call_id": "call-1", "summary": "Original."}}',
            }
        )

        result = await self._call(db)

        assert result["replay"] is True
        assert result["decision"] == stored
        db.set_completion_decision.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_new_tool_call_id_overwrites(self):
        db = self._db(
            {
                "id": "j1",
                "status": "processing",
                "context": {
                    "completion_decision": {
                        "tool_call_id": "call-0",
                        "summary": "Round 1.",
                    }
                },
            }
        )

        result = await self._call(db, confidence=1.7)  # clamped server-side

        assert result["replay"] is False
        decision = db.set_completion_decision.await_args.args[1]
        assert decision["tool_call_id"] == "call-1"
        assert decision["confidence"] == 1.0
        assert decision["summary"] == "Done."

    @pytest.mark.asyncio
    async def test_cas_loss_is_409(self):
        from fastapi import HTTPException

        db = self._db({"id": "j1", "status": "processing"})
        db.set_completion_decision = AsyncMock(return_value=False)

        with pytest.raises(HTTPException) as exc:
            await self._call(db)
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_durable_subagent_barrier_is_a_typed_useful_409(self):
        from fastapi import HTTPException
        from orchestrator import main

        db = self._db({"id": "j1", "status": "processing"})
        db.set_completion_decision = AsyncMock(
            side_effect=main.CompletionDecisionBlocked(
                live_subagents=2,
                queued_subagent_replies=1,
            )
        )

        with pytest.raises(HTTPException) as exc:
            await self._call(db)

        assert exc.value.status_code == 409
        assert exc.value.detail == {
            "code": "completion_decision_blocked",
            "reason": "live_subagents_and_queued_subagent_replies",
            "live_subagents": 2,
            "queued_subagent_replies": 1,
            "message": (
                "Job completion is blocked until every live subagent has "
                "settled and every queued subagent report has been consumed."
            ),
        }
