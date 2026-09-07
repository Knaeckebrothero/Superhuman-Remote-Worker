"""A job-ending push that does not land must be recorded, not swallowed.

`GitManager.push()` returns False on failure, and every job-ending caller in
`src/core/phase.py` used to discard it. A job whose deliverables never left the
pod therefore finished indistinguishable from one that delivered cleanly — it
reported success at confidence 1.0 and the pod was reclaimed with the only copy
of the work.

That is not hypothetical. A parser regression made *every* push fail for a
whole job, 26 times, and the incident was invisible for a day:
knowledge-history/done/git_push_fails_silently_via_workspace_backend.md (dev job
`40efbb39`). The push bug itself is fixed; this pins the consequence-handling,
so the next such regression is loud even though the cause will be different.

The push is deliberately NOT retried here — `push()` reports its own reason and
the pod is going away regardless. What matters is that the failure reaches the
freeze record the orchestrator stores, so the critic, the deliverable gate and
the cockpit can tell "empty because delivery failed" from "empty because the
agent produced nothing".
"""

import logging
from unittest.mock import MagicMock

from shared.runtime.core.loader import AgentConfig  # noqa: E402
from agent.core.phase import (  # noqa: E402
    DELIVERY_ERROR_KEY,
    DELIVERY_FAILED_KEY,
    finalize_job,
    freeze_for_review,
)
from agent.tools.core.job import (  # noqa: E402
    clear_final_phase_data,
    seed_final_phase_data,
)


def make_config(autonomy: str = "partial") -> AgentConfig:
    return AgentConfig(agent_id="test", display_name="Test", autonomy=autonomy)


def make_state(job_id: str = "test-job", phase_number: int = 1) -> dict:
    return {
        "job_id": job_id,
        "phase_number": phase_number,
        "is_strategic_phase": True,
        "messages": [],
    }


def make_workspace(*, pushed: bool, has_remote: bool = True) -> MagicMock:
    """A workspace whose git manager pushes (or doesn't) as instructed."""
    git = MagicMock()
    git.is_active = True
    git.has_remote = MagicMock(return_value=has_remote)
    git.push = MagicMock(return_value=pushed)
    git.push_ref = MagicMock(return_value=pushed)
    git.tag = MagicMock(return_value=True)
    git.commit = MagicMock(return_value=True)

    ws = MagicMock()
    ws.git_manager = git
    ws.get_head_commit = MagicMock(return_value="abc1234")
    return ws


def seed_final_data(job_id: str = "test-job") -> None:
    seed_final_phase_data(
        job_id,
        {
            "summary": "done",
            "deliverables": ["output/report.md"],
            "confidence": 1.0,
            "job_id": job_id,
        },
    )


class TestFinalizeJobRecordsDeliveryFailure:
    def setup_method(self):
        clear_final_phase_data("test-job")

    def teardown_method(self):
        clear_final_phase_data("test-job")

    def test_full_autonomy_completion_records_a_failed_push(self, caplog):
        """The autonomy=full branch: reports success, so it must report this."""
        seed_final_data()
        ws = make_workspace(pushed=False)

        with caplog.at_level(logging.ERROR):
            result = finalize_job(
                make_state(), ws, MagicMock(), config=make_config("full")
            )

        assert result.freeze_data[DELIVERY_FAILED_KEY] is True
        assert result.freeze_data[DELIVERY_ERROR_KEY]
        # Loud, not a warning buried among push chatter.
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_freeze_branch_records_a_failed_push(self, caplog):
        """The non-full 'freeze for review' branch."""
        seed_final_data()
        ws = make_workspace(pushed=False)

        with caplog.at_level(logging.ERROR):
            result = finalize_job(
                make_state(), ws, MagicMock(), config=make_config("partial")
            )

        assert result.freeze_data[DELIVERY_FAILED_KEY] is True

    def test_successful_push_leaves_no_marker(self):
        """Absence means delivered; nothing writes the key False."""
        seed_final_data()
        ws = make_workspace(pushed=True)

        result = finalize_job(make_state(), ws, MagicMock(), config=make_config("full"))

        assert DELIVERY_FAILED_KEY not in result.freeze_data
        assert DELIVERY_ERROR_KEY not in result.freeze_data

    def test_no_remote_is_not_a_delivery_failure(self):
        """push() also returns False when no remote is configured.

        Conflating "nothing to deliver to" with "delivery failed" would mark
        every remote-less job as lost — a false alarm on a legitimate
        configuration, which is worse than the silence being replaced.
        """
        seed_final_data()
        ws = make_workspace(pushed=False, has_remote=False)

        result = finalize_job(make_state(), ws, MagicMock(), config=make_config("full"))

        assert DELIVERY_FAILED_KEY not in result.freeze_data
        ws.git_manager.push.assert_not_called()

    def test_inactive_git_is_not_a_delivery_failure(self):
        """Git versioning off is a configuration, not a lost deliverable."""
        seed_final_data()
        ws = make_workspace(pushed=False)
        ws.git_manager.is_active = False

        result = finalize_job(make_state(), ws, MagicMock(), config=make_config("full"))

        assert DELIVERY_FAILED_KEY not in result.freeze_data


class TestFreezeForReviewRecordsDeliveryFailure:
    def test_boundary_freeze_records_a_failed_push(self):
        ws = make_workspace(pushed=False)

        result = freeze_for_review(
            make_state(),
            ws,
            MagicMock(),
            phase_type="strategic",
            phase_number=1,
        )

        assert result.freeze_data[DELIVERY_FAILED_KEY] is True

    def test_boundary_freeze_clean_push_leaves_no_marker(self):
        ws = make_workspace(pushed=True)

        result = freeze_for_review(
            make_state(),
            ws,
            MagicMock(),
            phase_type="strategic",
            phase_number=1,
        )

        assert DELIVERY_FAILED_KEY not in result.freeze_data
