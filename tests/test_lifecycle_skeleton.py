"""Smoke tests for the Phase 1a lifecycle module skeleton.

The full reconciliation algorithm and the first real manager
(`AgentInstanceManager`) land in Phase 1b. These tests only cover the
type surface the rest of the system will build against:

- `Instance` dataclass instantiation
- Protocol implementation checks (runtime_checkable)
- `DisruptionBudget.cap_for` math
- `InstanceLifecycleReconciler.is_drift` predicate edge cases
- `InstanceLifecycleReconciler.is_stateful` type narrowing
"""

from __future__ import annotations

import pytest

from orchestrator.services.lifecycle import (
    DisruptionBudget,
    Instance,
    InstanceLifecycleManager,
    InstanceLifecycleReconciler,
    StatefulInstanceManager,
)


# =============================================================================
# Instance dataclass
# =============================================================================


class TestInstance:
    def test_minimal_construction(self):
        inst = Instance(kind="agent", id="srw-agent-j-abc")
        assert inst.kind == "agent"
        assert inst.id == "srw-agent-j-abc"
        assert inst.version is None
        assert inst.bound_to is None
        assert inst.metadata == {}

    def test_full_construction(self):
        inst = Instance(
            kind="workspace",
            id="ws-thread-abc",
            version="b4d7fd1",
            bound_to="thread-uuid",
            metadata={"ssh_host": "10.0.0.1"},
        )
        assert inst.version == "b4d7fd1"
        assert inst.bound_to == "thread-uuid"
        assert inst.metadata["ssh_host"] == "10.0.0.1"

    def test_metadata_default_is_independent(self):
        # default_factory — two instances must not share the same dict.
        a = Instance(kind="agent", id="a")
        b = Instance(kind="agent", id="b")
        a.metadata["x"] = 1
        assert "x" not in b.metadata


# =============================================================================
# Protocol satisfaction (runtime_checkable)
# =============================================================================


class _FakeStateless:
    """Implements InstanceLifecycleManager."""

    kind = "agent"

    async def list_instances(self):
        return []

    async def expected_versions(self):
        return set()

    async def is_healthy(self, inst):
        return True

    async def is_idle(self, inst):
        return True

    async def signal_drain_pending(self, inst):
        return None

    async def drain(self, inst, grace_s):
        return None

    async def delete(self, inst, grace_s):
        return None


class _FakeStateful(_FakeStateless):
    """Implements StatefulInstanceManager — adds snapshot/restore."""

    kind = "workspace"

    async def snapshot(self, inst):
        return None

    async def restore(self, inst, snapshot_ref):
        return None


class TestProtocolSatisfaction:
    def test_stateless_satisfies_lifecycle_manager(self):
        assert isinstance(_FakeStateless(), InstanceLifecycleManager)

    def test_stateless_does_not_satisfy_stateful(self):
        assert not isinstance(_FakeStateless(), StatefulInstanceManager)

    def test_stateful_satisfies_both(self):
        m = _FakeStateful()
        assert isinstance(m, InstanceLifecycleManager)
        assert isinstance(m, StatefulInstanceManager)


# =============================================================================
# DisruptionBudget
# =============================================================================


class TestDisruptionBudget:
    def test_default_cap_is_quarter_with_floor_of_one(self):
        budget = DisruptionBudget()
        assert budget.cap_for("agent", total=10) == 2
        assert budget.cap_for("agent", total=4) == 1
        # Floor of 1 — even with one instance, drift detection can act.
        assert budget.cap_for("agent", total=1) == 1
        assert budget.cap_for("agent", total=0) == 1

    def test_per_kind_override(self):
        budget = DisruptionBudget(overrides={"workspace": 1, "agent": 5})
        assert budget.cap_for("workspace", total=20) == 1
        assert budget.cap_for("agent", total=20) == 5
        # Unconfigured kind falls through to default.
        assert budget.cap_for("vm", total=20) == 5

    def test_phase_1a_allow_is_unconditional(self):
        # Skeleton: allow returns True regardless. 1b adds in-flight tracking.
        budget = DisruptionBudget()
        assert budget.allow("agent") is True


# =============================================================================
# InstanceLifecycleReconciler
# =============================================================================


class TestReconciler:
    def test_register_appends_to_managers(self):
        reconciler = InstanceLifecycleReconciler()
        assert reconciler.managers == []
        m = _FakeStateless()
        reconciler.register(m)
        assert reconciler.managers == [m]

    def test_managers_property_returns_a_copy(self):
        reconciler = InstanceLifecycleReconciler(managers=[_FakeStateless()])
        snapshot = reconciler.managers
        snapshot.append(_FakeStateful())
        # Mutating the returned list must not affect the reconciler.
        assert len(reconciler.managers) == 1

    @pytest.mark.asyncio
    async def test_tick_returns_per_kind_stats(self):
        # Tick reports per-kind stats. With a FakeStateless that returns
        # no instances and no expected versions, every counter is 0.
        reconciler = InstanceLifecycleReconciler(managers=[_FakeStateless()])
        result = await reconciler.tick()
        assert result == {
            "agent": {
                "listed": 0,
                "unhealthy": 0,
                "drift": 0,
                "drained": 0,
                "skipped_busy": 0,
                "reaped": 0,
                "reap_attempts": 0,
                "reap_forced": 0,
            }
        }

    def test_is_stateful_narrows_correctly(self):
        assert InstanceLifecycleReconciler.is_stateful(_FakeStateful()) is True
        assert InstanceLifecycleReconciler.is_stateful(_FakeStateless()) is False

    def test_is_drift_skips_when_expected_empty(self):
        inst = Instance(kind="agent", id="x", version="abc")
        assert InstanceLifecycleReconciler.is_drift(inst, expected=set()) is False

    def test_is_drift_skips_when_version_unset(self):
        inst = Instance(kind="agent", id="x", version=None)
        assert InstanceLifecycleReconciler.is_drift(inst, expected={"abc"}) is False

    def test_is_drift_true_on_mismatch(self):
        inst = Instance(kind="agent", id="x", version="old")
        assert InstanceLifecycleReconciler.is_drift(inst, expected={"new"}) is True

    def test_is_drift_false_on_match(self):
        inst = Instance(kind="agent", id="x", version="abc")
        assert InstanceLifecycleReconciler.is_drift(inst, expected={"abc"}) is False

    def test_is_drift_false_when_version_in_multi_set(self):
        # Two valid SHAs (worker + persistent images differ briefly during
        # a partial rollout). An agent on either is current.
        inst = Instance(kind="agent", id="x", version="worker-sha")
        assert (
            InstanceLifecycleReconciler.is_drift(
                inst, expected={"worker-sha", "persistent-sha"}
            )
            is False
        )
