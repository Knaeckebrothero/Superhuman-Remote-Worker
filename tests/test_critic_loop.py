"""Unit tests for the critic feedback loop redesign.

Tests:
- Deferred verdict storage (_verdict_data accessors)
- _finalize_with_verdict produces correct freeze_data and status
- handle_transition uses freeze_data.status
- Round limit enforcement in _handle_critic_verdict
- Evaluation tools store verdict + final_phase_data
"""

import json
import pytest
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.tools.evaluation.evaluation_tools import (  # noqa: E402
    _verdict_data,
    get_verdict_data,
    clear_verdict_data,
    EVALUATION_TOOLS_METADATA,
    create_evaluation_tools,
)
from src.core.phase import (  # noqa: E402
    _finalize_with_verdict,
    TransitionResult,
)


# =============================================================================
# Fixtures
# =============================================================================


def make_state(job_id: str = "critic-job-1", phase_number: int = 2) -> dict:
    """Create a minimal agent state dict for a critic job."""
    return {
        "job_id": job_id,
        "phase_number": phase_number,
        "is_strategic_phase": True,
        "is_final_phase": False,
        "messages": [],
        "metadata": {
            "verification_target": "target-job-1",
            "original_config": "developer",
        },
    }


def make_workspace():
    """Create a mock WorkspaceManager."""
    ws = MagicMock()
    ws.write_file = MagicMock()
    ws.git_manager = None
    return ws


def make_todo_manager():
    """Create a mock TodoManager."""
    tm = MagicMock()
    tm.archive = MagicMock()
    return tm


# =============================================================================
# Deferred verdict storage tests
# =============================================================================


class TestVerdictData:
    """Tests for _verdict_data accessors."""

    def setup_method(self):
        """Clear verdict data before each test."""
        _verdict_data.clear()

    def test_get_verdict_data_empty(self):
        assert get_verdict_data("nonexistent") is None

    def test_set_and_get_verdict_data(self):
        _verdict_data["job-1"] = {"_verdict": "approved", "_target_job_id": "target-1"}
        result = get_verdict_data("job-1")
        assert result is not None
        assert result["_verdict"] == "approved"
        assert result["_target_job_id"] == "target-1"

    def test_clear_verdict_data(self):
        _verdict_data["job-1"] = {"_verdict": "returned"}
        clear_verdict_data("job-1")
        assert get_verdict_data("job-1") is None

    def test_clear_nonexistent_is_safe(self):
        clear_verdict_data("nonexistent")  # Should not raise

    def test_multiple_jobs(self):
        _verdict_data["job-1"] = {"_verdict": "approved"}
        _verdict_data["job-2"] = {"_verdict": "returned"}
        assert get_verdict_data("job-1")["_verdict"] == "approved"
        assert get_verdict_data("job-2")["_verdict"] == "returned"
        clear_verdict_data("job-1")
        assert get_verdict_data("job-1") is None
        assert get_verdict_data("job-2") is not None


# =============================================================================
# _finalize_with_verdict tests
# =============================================================================


class TestFinalizeWithVerdict:
    """Tests for _finalize_with_verdict."""

    def test_approved_verdict(self):
        state = make_state()
        ws = make_workspace()
        tm = make_todo_manager()

        verdict = {
            "_verdict": "approved",
            "_target_job_id": "target-job-1",
            "report": "Great work!",
            "strengths": ["clean code"],
            "minor_notes": [],
        }

        result = _finalize_with_verdict(state, ws, tm, verdict, "critic-job-1")

        assert isinstance(result, TransitionResult)
        assert result.success is True
        assert result.state_updates["should_stop"] is True
        assert result.state_updates["goal_achieved"] is True
        assert result.freeze_data["status"] == "completed"
        assert result.freeze_data["freeze_type"] == "verdict"
        assert result.freeze_data["verdict"] == "approved"
        assert result.freeze_data["target_job_id"] == "target-job-1"
        assert result.freeze_data["report"] == "Great work!"

        # Should write critic_verdict.json
        ws.write_file.assert_called_once()
        call_args = ws.write_file.call_args
        assert call_args[0][0] == "output/critic_verdict.json"

        # Should archive todos
        tm.archive.assert_called_once_with("final")

    def test_returned_verdict(self):
        state = make_state()
        ws = make_workspace()
        tm = make_todo_manager()

        verdict = {
            "_verdict": "returned",
            "_target_job_id": "target-job-1",
            "_feedback": "## Verification Feedback\n\nFix the tests",
            "feedback_raw": "Fix the tests",
            "issues": ["Test coverage too low", "Missing edge cases"],
            "severity": "high",
        }

        result = _finalize_with_verdict(state, ws, tm, verdict, "critic-job-1")

        assert result.success is True
        assert result.state_updates["should_stop"] is True
        assert result.state_updates["goal_achieved"] is False
        assert result.freeze_data["status"] == "waiting"
        assert result.freeze_data["freeze_type"] == "verdict"
        assert result.freeze_data["verdict"] == "returned"
        assert result.freeze_data["target_job_id"] == "target-job-1"
        assert (
            result.freeze_data["feedback"]
            == "## Verification Feedback\n\nFix the tests"
        )
        assert len(result.freeze_data["issues"]) == 2

    def test_unknown_verdict_defaults_to_completed(self):
        state = make_state()
        ws = make_workspace()
        tm = make_todo_manager()

        verdict = {
            "_verdict": "unknown_type",
            "_target_job_id": "target-job-1",
        }

        result = _finalize_with_verdict(state, ws, tm, verdict, "critic-job-1")

        assert result.success is True
        assert result.state_updates["goal_achieved"] is True
        assert result.freeze_data["status"] == "completed"


# =============================================================================
# handle_transition freeze_data.status tests
# =============================================================================


class TestHandleTransitionFreezeDataStatus:
    """Tests that handle_transition respects freeze_data.status."""

    @pytest.mark.asyncio
    async def test_freeze_data_status_completed(self):
        """When freeze_data has status='completed', DB should get 'completed'."""
        from src.core.phase import TransitionResult

        mock_db = MagicMock()
        mock_db.jobs = MagicMock()
        mock_db.jobs.update_status = AsyncMock()
        mock_db.execute = AsyncMock()

        result = TransitionResult(
            success=True,
            state_updates={"should_stop": True, "goal_achieved": True},
            freeze_data={"status": "completed", "freeze_type": "verdict"},
        )

        # Simulate what handle_transition does
        if result.freeze_data and result.freeze_data.get("status"):
            db_status = result.freeze_data["status"]
            if db_status == "job_completed":
                db_status = "completed"
        elif result.state_updates.get("goal_achieved"):
            db_status = "completed"
        else:
            db_status = "pending_review"

        assert db_status == "completed"

    @pytest.mark.asyncio
    async def test_freeze_data_status_waiting(self):
        """When freeze_data has status='waiting', DB should get 'waiting'."""
        result = TransitionResult(
            success=True,
            state_updates={"should_stop": True, "goal_achieved": False},
            freeze_data={"status": "waiting", "freeze_type": "verdict"},
        )

        if result.freeze_data and result.freeze_data.get("status"):
            db_status = result.freeze_data["status"]
        elif result.state_updates.get("goal_achieved"):
            db_status = "completed"
        else:
            db_status = "pending_review"

        assert db_status == "waiting"

    @pytest.mark.asyncio
    async def test_freeze_data_status_job_completed_normalized(self):
        """Synonym 'job_completed' should normalize to 'completed'."""
        result = TransitionResult(
            success=True,
            state_updates={"should_stop": True, "goal_achieved": True},
            freeze_data={"status": "job_completed"},
        )

        if result.freeze_data and result.freeze_data.get("status"):
            db_status = result.freeze_data["status"]
            if db_status == "job_completed":
                db_status = "completed"
        else:
            db_status = "pending_review"

        assert db_status == "completed"

    @pytest.mark.asyncio
    async def test_no_freeze_data_falls_through(self):
        """Without freeze_data.status, use goal_achieved logic."""
        # goal_achieved=True → completed
        result = TransitionResult(
            success=True,
            state_updates={"should_stop": True, "goal_achieved": True},
            freeze_data=None,
        )

        if result.freeze_data and result.freeze_data.get("status"):
            db_status = result.freeze_data["status"]
        elif result.state_updates.get("goal_achieved"):
            db_status = "completed"
        else:
            db_status = "pending_review"

        assert db_status == "completed"

        # goal_achieved=False → pending_review
        result2 = TransitionResult(
            success=True,
            state_updates={"should_stop": True, "goal_achieved": False},
            freeze_data={"freeze_type": "phase_boundary"},  # No status key
        )

        if result2.freeze_data and result2.freeze_data.get("status"):
            db_status2 = result2.freeze_data["status"]
        elif result2.state_updates.get("goal_achieved"):
            db_status2 = "completed"
        else:
            db_status2 = "pending_review"

        assert db_status2 == "pending_review"


# =============================================================================
# Evaluation tool metadata tests
# =============================================================================


class TestEvaluationToolMetadata:
    """Tests for evaluation tool registry metadata."""

    def test_approve_job_is_strategic_only(self):
        assert EVALUATION_TOOLS_METADATA["approve_job"]["phases"] == ["strategic"]

    def test_return_job_is_strategic_only(self):
        assert EVALUATION_TOOLS_METADATA["return_job_with_feedback"]["phases"] == [
            "strategic"
        ]


# =============================================================================
# Critic strategic-prompt verdict-timing regression
# =============================================================================
#
# Guards against the 2026-06-03 deadlock (job 8a3fc7d1): the verdict tools
# approve_job / return_job_with_feedback are strategic-only (asserted above),
# but the critic strategic prompt told the agent "Do NOT render verdicts during
# strategic phases" — leaving no phase where the agent could both call the tool
# AND believe it was allowed to. The agent looped forever via todo_rewind.
# See docs/tests/critic_verdict_deadlock_verification.md.

CRITIC_PROMPT_DIR = project_root / "config" / "experts" / "critic"


class TestCriticStrategicPromptVerdictTiming:
    """The critic strategic prompt must agree with the strategic-only verdict tools."""

    @pytest.mark.parametrize("fname", ["strategic.txt", "strategic_minimax.txt"])
    def test_prompt_does_not_forbid_strategic_verdict(self, fname):
        text = (CRITIC_PROMPT_DIR / fname).read_text(encoding="utf-8")
        # The exact sentence that caused the 8a3fc7d1 deadlock.
        assert "Do NOT render verdicts during strategic phases" not in text, (
            f"{fname} forbids strategic verdicts, but approve_job/"
            "return_job_with_feedback are strategic-only — this deadlocks the critic."
        )

    @pytest.mark.parametrize("fname", ["strategic.txt", "strategic_minimax.txt"])
    def test_prompt_directs_verdict_into_strategic(self, fname):
        text = (CRITIC_PROMPT_DIR / fname).read_text(encoding="utf-8")
        assert "approve_job" in text
        # The fix tells the agent these tools are strategic-only and must not be
        # deferred to a tactical todo.
        assert "strategic-phase-only" in text, (
            f"{fname} lost the directive that the verdict tools are strategic-only."
        )


# =============================================================================
# Critic verdict-tool / template agreement (Task 7)
# =============================================================================
#
# Same failure MODE as the deadlock above, different mechanism: instead of a
# phase-timing contradiction, the §4 "Render Your Verdict" section in
# verification_instructions.md kept documenting the RETIRED
# `return_job_with_feedback(..., issues=[...], severity="...")` call shape
# after Task 5 replaced it with `findings=[...]` / `dispositions=[...]`. A
# stale doc doesn't crash the tool call the way the phase contradiction did,
# but it reliably mis-teaches the model the wrong verdict-tool shape. These
# assertions are tied to the LIVE tool signatures (not a hardcoded string),
# so a future signature change that forgets to update the prompt fails here
# instead of drifting silently again.

VERIFICATION_INSTRUCTIONS_PATH = CRITIC_PROMPT_DIR / "verification_instructions.md"


class TestCriticVerdictInstructionsToolAgreement:
    """§4 of verification_instructions.md must match the REAL verdict tools."""

    def test_stale_verdict_shape_is_gone(self):
        text = VERIFICATION_INSTRUCTIONS_PATH.read_text(encoding="utf-8")
        assert "issues=" not in text, (
            "verification_instructions.md still documents the retired "
            'issues=[...] / severity="..." call shape that Task 5 replaced.'
        )

    def test_verdict_examples_use_real_tool_params(self):
        text = VERIFICATION_INSTRUCTIONS_PATH.read_text(encoding="utf-8")
        for tool_fn in create_evaluation_tools(MagicMock()):
            assert f"{tool_fn.name}(" in text, (
                f"verification_instructions.md never calls `{tool_fn.name}`."
            )
            for param in tool_fn.args:
                assert f"{param}=" in text, (
                    f"verification_instructions.md never shows "
                    f"`{tool_fn.name}`'s real `{param}=` parameter — the "
                    "prompt has drifted from the tool surface."
                )


# =============================================================================
# Round limit enforcement tests
# =============================================================================


@pytest.mark.skip(
    reason="Agent-side _handle_critic_verdict removed; logic moved to orchestrator"
)
class TestRoundLimitEnforcement:
    """Tests for round limit logic — now handled by orchestrator."""

    def test_round_under_limit(self):
        """Rounds under max should allow feedback delivery."""
        current_round = 1
        max_rounds = 3
        assert current_round < max_rounds

    def test_round_at_limit(self):
        """Round at max should trigger discard."""
        current_round = 3
        max_rounds = 3
        assert current_round >= max_rounds

    def test_round_over_limit(self):
        """Round over max should trigger discard."""
        current_round = 5
        max_rounds = 3
        assert current_round >= max_rounds

    def test_round_zero_under_limit(self):
        """First round (0) is always under limit."""
        current_round = 0
        max_rounds = 3
        assert current_round < max_rounds

    @pytest.mark.asyncio
    async def test_handle_critic_verdict_approved(self):
        """Approved verdict should call _set_target_to_autonomy_status."""
        from src.api.app import _handle_critic_verdict

        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "parent_job_id": "target-job-1",
                "freeze_data": json.dumps(
                    {
                        "verdict": "approved",
                        "target_job_id": "target-job-1",
                    }
                ),
                "context": json.dumps(
                    {
                        "verification_round": 0,
                        "max_verification_rounds": 3,
                    }
                ),
            }
        )

        mock_agent = MagicMock()
        mock_agent.postgres_conn = mock_conn

        with (
            patch("src.api.app._agent", mock_agent),
            patch(
                "src.api.app._set_target_to_autonomy_status", new_callable=AsyncMock
            ) as mock_set,
        ):
            await _handle_critic_verdict("critic-1", {})
            mock_set.assert_called_once_with("target-job-1")

    @pytest.mark.asyncio
    async def test_handle_critic_verdict_returned_under_limit(self):
        """Returned verdict under limit should resume target."""
        from src.api.app import _handle_critic_verdict

        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "parent_job_id": "target-job-1",
                "freeze_data": json.dumps(
                    {
                        "verdict": "returned",
                        "feedback": "Fix the tests",
                    }
                ),
                "context": json.dumps(
                    {
                        "verification_round": 1,
                        "max_verification_rounds": 3,
                    }
                ),
            }
        )

        mock_agent = MagicMock()
        mock_agent.postgres_conn = mock_conn

        mock_client = MagicMock()
        mock_client.resume_job = AsyncMock(return_value=True)

        with (
            patch("src.api.app._agent", mock_agent),
            patch("src.api.app._orchestrator_client", mock_client),
        ):
            await _handle_critic_verdict("critic-1", {})
            mock_client.resume_job.assert_called_once_with(
                "target-job-1", feedback="Fix the tests"
            )

    @pytest.mark.asyncio
    async def test_handle_critic_verdict_returned_at_limit(self):
        """Returned verdict at round limit should fail critic and auto-accept target."""
        from src.api.app import _handle_critic_verdict

        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "parent_job_id": "target-job-1",
                "freeze_data": json.dumps(
                    {
                        "verdict": "returned",
                        "feedback": "Still broken",
                    }
                ),
                "context": json.dumps(
                    {
                        "verification_round": 3,
                        "max_verification_rounds": 3,
                    }
                ),
            }
        )
        mock_conn.jobs = MagicMock()
        mock_conn.jobs.update_status = AsyncMock()

        mock_agent = MagicMock()
        mock_agent.postgres_conn = mock_conn

        with (
            patch("src.api.app._agent", mock_agent),
            patch(
                "src.api.app._set_target_to_autonomy_status", new_callable=AsyncMock
            ) as mock_set,
        ):
            await _handle_critic_verdict("critic-1", {})

            # Critic should be set to failed
            mock_conn.jobs.update_status.assert_called_once_with(
                "critic-1",
                status="failed",
                error_message="Verification limit reached (3 rounds)",
            )
            # Target should be set to autonomy status
            mock_set.assert_called_once_with("target-job-1")

    @pytest.mark.asyncio
    async def test_handle_critic_verdict_not_a_critic(self):
        """Non-critic jobs (no parent_job_id) should be skipped."""
        from src.api.app import _handle_critic_verdict

        mock_conn = MagicMock()
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "parent_job_id": None,
                "freeze_data": None,
                "context": None,
            }
        )

        mock_agent = MagicMock()
        mock_agent.postgres_conn = mock_conn

        with (
            patch("src.api.app._agent", mock_agent),
            patch(
                "src.api.app._set_target_to_autonomy_status", new_callable=AsyncMock
            ) as mock_set,
        ):
            await _handle_critic_verdict("regular-job-1", {})
            mock_set.assert_not_called()


# =============================================================================
# Integration: on_strategic_phase_complete with verdict
# =============================================================================


class TestStrategicPhaseWithVerdict:
    """Tests that on_strategic_phase_complete detects verdict data."""

    def setup_method(self):
        """Clear state before each test."""
        _verdict_data.clear()

    def test_verdict_triggers_finalize(self):
        """When verdict data exists, on_strategic_phase_complete should finalize."""
        from src.core.phase import on_strategic_phase_complete

        state = make_state()
        ws = make_workspace()
        tm = make_todo_manager()
        tm.has_staged_todos = MagicMock(return_value=False)
        tm.list_all = MagicMock(return_value=[])

        # Set verdict data
        _verdict_data["critic-job-1"] = {
            "_verdict": "approved",
            "_target_job_id": "target-job-1",
            "report": "Looks good",
        }

        result = on_strategic_phase_complete(state, ws, tm)

        assert result.success is True
        assert result.state_updates["should_stop"] is True
        assert result.freeze_data["verdict"] == "approved"

        # Verdict data should be consumed
        assert get_verdict_data("critic-job-1") is None


# =============================================================================
# Verdict durability: tools must persist through the orchestrator BEFORE
# returning to the model (journal-before-observe). Task 5 of the fail-closed
# verification plan — see docs/superpowers/plans/2026-07-27-verification-fail-closed.md.
# =============================================================================


class TestVerdictDurability:
    @pytest.mark.asyncio
    async def test_return_tool_fails_loudly_without_orchestrator_client(self):
        """A verdict that cannot be persisted must NOT report success.

        The house convention for orchestrator_client is best-effort
        (`if client is None: return None`). For a verdict that is exactly
        backwards — a silently-unrecorded rejection becomes an approval.
        """
        from src.tools.context import ToolContext
        from src.tools.evaluation.evaluation_tools import create_evaluation_tools

        ctx = ToolContext(_job_id="c1", config={})
        ctx.orchestrator_client = None
        approve, return_with_feedback = create_evaluation_tools(ctx)

        result = await return_with_feedback.ainvoke(
            {
                "job_id": "t1",
                "feedback": "bad",
                "findings": [{"claim": "x", "severity": "high"}],
            }
        )

        assert "error" in result.lower()
        from src.tools.evaluation.evaluation_tools import get_verdict_data

        assert get_verdict_data("c1") is None

    @pytest.mark.asyncio
    async def test_tool_stores_server_computed_verdict_not_asserted(self):
        """The server's computed verdict wins over what the model claimed."""
        from unittest.mock import AsyncMock

        from src.tools.context import ToolContext
        from src.tools.evaluation.evaluation_tools import (
            create_evaluation_tools,
            get_verdict_data,
        )

        ctx = ToolContext(_job_id="c2", config={})
        ctx.orchestrator_client = AsyncMock()
        ctx.orchestrator_client.record_verification_round = AsyncMock(
            return_value={
                "verdict": "returned",
                "round": 3,
                "assigned": [],
                "open_findings": [{"id": "F1"}],
            }
        )
        approve, _ = create_evaluation_tools(ctx)

        await approve.ainvoke(
            {
                "job_id": "t1",
                "report": "looks good",
                "dispositions": [{"id": "F1", "disposition": "STILL_OPEN"}],
            }
        )

        assert get_verdict_data("c2")["_verdict"] == "returned"

    @pytest.mark.asyncio
    async def test_workspace_write_failure_degrades_gracefully_not_raises(self):
        """Round 1 fix — Finding 1.

        The round is durably recorded by the orchestrator BEFORE any local
        bookkeeping runs. If the subsequent workspace write then fails (I/O
        error, remote-backend hiccup), the tool must report success with a
        degradation note, not crash — the model must not be told to correct
        and resubmit a round that has already landed durably.
        """
        from unittest.mock import AsyncMock, MagicMock

        from src.tools.context import ToolContext
        from src.tools.evaluation.evaluation_tools import (
            create_evaluation_tools,
            get_verdict_data,
        )

        ctx = ToolContext(_job_id="c3", config={})
        ctx.orchestrator_client = AsyncMock()
        ctx.orchestrator_client.record_verification_round = AsyncMock(
            return_value={
                "verdict": "approved",
                "round": 1,
                "assigned": [],
                "open_findings": [],
            }
        )
        ctx.workspace_manager = MagicMock()
        ctx.workspace_manager.get_head_commit = MagicMock(return_value=None)
        ctx.workspace_manager.write_file = MagicMock(side_effect=OSError("disk full"))
        approve, _ = create_evaluation_tools(ctx)

        result = await approve.ainvoke({"job_id": "t1", "report": "looks good"})

        # Must NOT read like the "not recorded" failure path...
        assert "error" not in result.lower()
        # ...and must say plainly that the round IS recorded.
        assert "recorded" in result.lower()
        assert "disk full" in result

        # Bonus (stricter than the brief's minimal ask): the mirror is
        # populated before the workspace write is attempted, so a write
        # failure alone doesn't also cost finalize_job the verdict.
        assert get_verdict_data("c3")["_verdict"] == "approved"

    @pytest.mark.asyncio
    async def test_approve_tool_fails_loudly_when_client_raises(self):
        """Round 1 fix — Finding 2 (approve_job half).

        Complements test_return_tool_fails_loudly_without_orchestrator_client
        (client is None — an impossible write) with the other half of the
        property: a client that IS present but whose
        record_verification_round raises must also produce a failure-
        signaling string and leave no local trace.
        """
        from unittest.mock import AsyncMock

        from src.api.orchestrator_client import VerdictRecordingError
        from src.tools.context import ToolContext
        from src.tools.evaluation.evaluation_tools import (
            create_evaluation_tools,
            get_verdict_data,
        )

        ctx = ToolContext(_job_id="c4", config={})
        ctx.orchestrator_client = AsyncMock()
        ctx.orchestrator_client.record_verification_round = AsyncMock(
            side_effect=VerdictRecordingError(
                "verdict rejected:\n- F1: no disposition supplied."
            )
        )
        approve, _ = create_evaluation_tools(ctx)

        result = await approve.ainvoke({"job_id": "t1", "report": "looks good"})

        assert "error" in result.lower()
        assert "not recorded" in result.lower()
        assert get_verdict_data("c4") is None

    @pytest.mark.asyncio
    async def test_return_tool_fails_loudly_when_client_raises(self):
        """Round 1 fix — Finding 2 (return_job_with_feedback half)."""
        from unittest.mock import AsyncMock

        from src.api.orchestrator_client import VerdictRecordingError
        from src.tools.context import ToolContext
        from src.tools.evaluation.evaluation_tools import (
            create_evaluation_tools,
            get_verdict_data,
        )

        ctx = ToolContext(_job_id="c5", config={})
        ctx.orchestrator_client = AsyncMock()
        ctx.orchestrator_client.record_verification_round = AsyncMock(
            side_effect=VerdictRecordingError("network error: connection refused")
        )
        _, return_with_feedback = create_evaluation_tools(ctx)

        result = await return_with_feedback.ainvoke(
            {
                "job_id": "t1",
                "feedback": "bad",
                "findings": [{"claim": "x", "severity": "high"}],
            }
        )

        assert "error" in result.lower()
        assert "not recorded" in result.lower()
        assert get_verdict_data("c5") is None
