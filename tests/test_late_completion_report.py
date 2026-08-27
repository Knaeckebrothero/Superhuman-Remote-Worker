"""A finished job must not stay failed because its report arrived late.

Regression guard for Defect 2 of
knowledge-base/knowledge/issues/transient_db_error_hard_fails_job_and_destroys_vm.md.

The ``/complete`` gate rejected any report on a terminal job *before* looking
at it, which stranded two real cases on 2026-07-27:

  * job ``e1192a9d`` finished, was failed out-of-band by a Postgres disk-full
    error, and its ``job_complete`` freeze 400'd — so it stayed ``failed``
    despite having written every deliverable. It had to be repaired by hand.
  * job ``c6dd288d`` filed a correct, recoverable ``workspace_unavailable``
    report which never reached the recovery arm 47 lines below the gate.

This authorises exactly the first case. The second stays rejected — but must
now be logged, because the silence is what hid the gate through two incidents.
"""

import json
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.completion import (  # noqa: E402
    determine_job_status,
    is_late_completion_report,
)

COMPLETION_FREEZE = {"freeze_type": "job_complete", "summary": "done"}


def _job(status="failed", **kw):
    return {"id": "job-1", "status": status, **kw}


class TestAuthorisedCase:
    def test_completion_freeze_on_failed_job_is_accepted(self):
        """The e1192a9d shape."""
        assert is_late_completion_report(
            _job("failed"), {"freeze_data": COMPLETION_FREEZE}
        )

    def test_full_autonomy_freeze_shape_is_also_accepted(self):
        """Autonomy 'full' writes status=job_completed with no freeze_type."""
        assert is_late_completion_report(
            _job("failed"), {"freeze_data": {"status": "job_completed"}}
        )

    def test_freeze_serialised_as_json_string_is_accepted(self):
        assert is_late_completion_report(
            _job("failed"), {"freeze_data": json.dumps(COMPLETION_FREEZE)}
        )


class TestRefusedCases:
    def test_cancelled_is_never_overridden(self):
        """Explicit human intent is not reversed by a late machine report."""
        assert not is_late_completion_report(
            _job("cancelled"), {"freeze_data": COMPLETION_FREEZE}
        )

    def test_recoverable_error_cannot_reopen_a_failed_job(self):
        """Re-resolve, never re-open — re-dispatching an already-run job is the
        hazard that forced c6dd288d to be reconstructed rather than resumed."""
        assert not is_late_completion_report(
            _job("failed"),
            {"error": {"type": "workspace_unavailable", "recoverable": True}},
        )

    def test_phase_boundary_freeze_is_not_a_completion(self):
        assert not is_late_completion_report(
            _job("failed"), {"freeze_data": {"freeze_type": "phase_boundary"}}
        )

    def test_no_freeze_at_all(self):
        assert not is_late_completion_report(_job("failed"), {})

    def test_malformed_freeze_json_is_refused_not_raised(self):
        assert not is_late_completion_report(
            _job("failed"), {"freeze_data": "{not json"}
        )

    @pytest.mark.parametrize("status", ["processing", "reviewing", "pending_review"])
    def test_non_terminal_jobs_are_not_this_functions_business(self, status):
        """Those pass the gate normally; this predicate must not claim them."""
        assert not is_late_completion_report(
            _job(status), {"freeze_data": COMPLETION_FREEZE}
        )


class TestResolutionOutcome:
    def test_accepted_report_resolves_to_pending_review(self):
        """Acceptance is only useful if the downstream resolution is right.

        A job_complete freeze WITHOUT goal_achieved is job e1192a9d's shape:
        the agent froze for human review rather than declaring itself done.
        """
        job = _job("failed")
        new_status, error = determine_job_status(
            job, {"freeze_data": COMPLETION_FREEZE, "should_stop": True}
        )
        assert new_status == "pending_review"
        assert error is None

    def test_goal_achieved_completion_resolves_terminally_not_to_failed(self):
        """The other autonomy shape still must not stay failed."""
        job = _job("failed")
        new_status, _ = determine_job_status(
            job,
            {
                "freeze_data": COMPLETION_FREEZE,
                "should_stop": True,
                "goal_achieved": True,
            },
        )
        assert new_status == "completed"

    def test_a_genuine_error_still_fails_even_with_a_completion_freeze(self):
        """Re-resolution must not launder a real mid-run crash into success."""
        job = _job("failed")
        new_status, error = determine_job_status(
            job,
            {
                "freeze_data": COMPLETION_FREEZE,
                "should_stop": True,
                "error": {"message": "genuine crash", "type": "job_error"},
            },
        )
        assert new_status == "failed"
        assert "genuine crash" in (error or "")
