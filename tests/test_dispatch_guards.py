"""Unit tests for the dispatcher preemption placeability guard (D1).

Covers the pure predicate ``preemption_blocked_reason``. The dispatcher itself
(``_try_dispatch_pending_jobs`` in orchestrator/main.py) is untested, so the
decision logic is extracted here to be verified in isolation. See
docs/issues/preemption_before_first_checkpoint_replays_job_opening.md.
"""

from __future__ import annotations

from orchestrator.services.dispatch_guards import preemption_blocked_reason


class TestPreemptionBlockedReason:
    def test_root_job_never_blocked(self):
        # No parent_job_id → a normal job; never blocked regardless of the
        # (irrelevant) parent_status argument.
        assert preemption_blocked_reason({"id": "j1"}, None) is None
        assert preemption_blocked_reason({"id": "j1"}, "completed") is None

    def test_subjob_with_live_parent_not_blocked(self):
        job = {"id": "critic1", "parent_job_id": "p1"}
        for parent_status in (
            "processing",
            "reviewing",
            "waiting",
            "created",
            "paused",
        ):
            assert preemption_blocked_reason(job, parent_status) is None

    def test_subjob_with_terminal_parent_blocked(self):
        job = {"id": "critic1", "parent_job_id": "p1"}
        for parent_status in ("completed", "failed", "cancelled"):
            reason = preemption_blocked_reason(job, parent_status)
            assert reason is not None
            assert parent_status in reason

    def test_subjob_with_missing_parent_not_blocked(self):
        # Parent not found (None) → cannot confirm terminal; let other guards /
        # the sweeper handle it rather than blocking on uncertainty.
        job = {"id": "critic1", "parent_job_id": "p1"}
        assert preemption_blocked_reason(job, None) is None
