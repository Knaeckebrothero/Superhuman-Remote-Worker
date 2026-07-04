"""Unit tests for the dispatcher preemption placeability guard (D1).

Covers the pure predicate ``preemption_blocked_reason``. The dispatcher itself
(``_try_dispatch_pending_jobs`` in orchestrator/main.py) is untested, so the
decision logic is extracted here to be verified in isolation. See
docs/done/preemption_before_first_checkpoint_replays_job_opening.md.
"""

from __future__ import annotations

from orchestrator.services.dispatch_guards import (
    VM_PARK_EXHAUSTED,
    VM_PARKED,
    VM_PROVISION,
    VM_READY,
    VM_RECYCLE,
    VM_WAIT,
    preemption_blocked_reason,
    vm_provisioning_decision,
)


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


class TestVmProvisioningDecision:
    """The VM provisioning state machine (timeout + bounded-retry park)."""

    def _decide(self, vm_ctx, *, attempts=0, cap=3, now=1000.0, timeout_s=600.0):
        return vm_provisioning_decision(
            vm_ctx,
            provision_attempts=attempts,
            max_provision_attempts=cap,
            now=now,
            timeout_s=timeout_s,
        )

    def test_absent_provisions(self):
        assert self._decide({}) == VM_PROVISION

    def test_deleted_re_provisions(self):
        # The drain/crash-recovery release leaves 'deleted' — must re-provision.
        assert self._decide({"status": "deleted"}) == VM_PROVISION

    def test_absent_parks_when_attempts_exhausted(self):
        assert self._decide({}, attempts=3, cap=3) == VM_PARK_EXHAUSTED
        assert self._decide({"status": "deleted"}, attempts=5, cap=3) == (
            VM_PARK_EXHAUSTED
        )

    def test_failed_stays_parked(self):
        # Never hot-retry a failed VM against the shared cluster.
        assert self._decide({"status": "failed"}) == VM_PARKED

    def test_deleting_waits(self):
        assert self._decide({"status": "deleting"}) == VM_WAIT

    def test_ready_dispatches(self):
        assert self._decide({"status": "ready"}) == VM_READY

    def test_provisioning_within_budget_waits(self):
        # 'created' 100s ago, 600s budget → still booting, wait.
        ctx = {"status": "created", "provisioned_at": 900.0}
        assert self._decide(ctx, now=1000.0, timeout_s=600.0) == VM_WAIT

    def test_provisioning_past_budget_recycles(self):
        # 'created' 700s ago, 600s budget → stuck, recycle.
        ctx = {"status": "created", "provisioned_at": 300.0}
        assert self._decide(ctx, now=1000.0, timeout_s=600.0) == VM_RECYCLE

    def test_provisioning_without_timestamp_waits_forever(self):
        # A VM created before provisioned_at existed has no anchor → wait (old
        # behaviour), never spuriously recycled.
        assert self._decide({"status": "creating"}) == VM_WAIT

    def test_recycle_needs_both_status_and_stale_timestamp(self):
        # 'ready' is never recycled even with an old timestamp.
        ctx = {"status": "ready", "provisioned_at": 1.0}
        assert self._decide(ctx, now=10_000.0, timeout_s=600.0) == VM_READY
