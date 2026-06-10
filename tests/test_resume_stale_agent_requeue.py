"""Fix 2 for the worker-pod-state zombie bug.

A 409 from the agent on resume means its DB ``status='ready'`` was stale — the
pod is actually non-idle and refused the resume. The orchestrator should
re-queue the job for auto-dispatch instead of surfacing a 502; any other
non-2xx stays a 502. This pins that decision (``_resume_reject_should_requeue``)
as real code. See docs/done/worker_pod_state_zombie_on_cancel.md.
"""

from __future__ import annotations


class TestResumeRejectShouldRequeue:
    def test_409_requeues(self):
        import orchestrator.main as om

        assert om._resume_reject_should_requeue(409) is True

    def test_other_non_2xx_stay_502(self):
        import orchestrator.main as om

        for code in (400, 403, 404, 500, 502, 503):
            assert om._resume_reject_should_requeue(code) is False, code
