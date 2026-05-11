"""Generic lifecycle reconciler — Phase 1a skeleton.

The reconciler iterates registered managers on each tick and reconciles
each managed instance against the manager's expected version and health
predicates. This module defines the shape; the actual tick loop and the
wiring into ``orchestrator.main.lifespan`` land in Phase 1b alongside
the first real ``InstanceLifecycleManager`` implementation
(``AgentInstanceManager``).

Disruption budget is the rate limiter that prevents a rollout from
draining too many instances of one kind at once. Karpenter and the K8s
Eviction API both rate-limit for the same reason — without it,
drift-detection fires N concurrent drains the moment a new image lands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .types import (
    Instance,
    InstanceLifecycleManager,
    StatefulInstanceManager,
)

logger = logging.getLogger(__name__)


@dataclass
class DisruptionBudget:
    """Caps concurrent drains per instance kind.

    Default per-kind cap is ``max(1, total // 4)`` of the kind's current
    instance count, mirroring Karpenter's "max 25% concurrent disruption"
    convention. Configurable via Helm values; the reconciler asks the
    budget whether it can act on a kind at the start of each candidate's
    drain decision.

    Phase 1a: shape only. The actual concurrency tracking lands in 1b
    when the reconciler is wired into the event loop.
    """

    overrides: dict[str, int] | None = None

    def cap_for(self, kind: str, total: int) -> int:
        if self.overrides and kind in self.overrides:
            return self.overrides[kind]
        return max(1, total // 4)

    def allow(self, kind: str) -> bool:
        # Phase 1a: always allow. 1b adds in-flight accounting.
        return True


class InstanceLifecycleReconciler:
    """Generic lifecycle reconciler.

    Phase 1a: skeleton. Owns the manager registry, exposes ``tick()``
    as a no-op. Phase 1b implements the reconciliation algorithm and
    wires startup reconciliation into the orchestrator lifespan.

    The reconciler does NOT own:
      - Heartbeat handling (lives on the agents heartbeat endpoint).
      - Warm pool maintenance (kind-specific; agent pool reconciler).
      - Adoption / job binding (lives in the dispatcher).
      - Snapshot bucket management (delegated to SnapshotService).
    """

    def __init__(
        self,
        managers: list[InstanceLifecycleManager] | None = None,
        budget: DisruptionBudget | None = None,
    ):
        self._managers: list[InstanceLifecycleManager] = managers or []
        self._budget = budget or DisruptionBudget()

    def register(self, manager: InstanceLifecycleManager) -> None:
        """Add a manager to the reconciler's registry.

        Order does not matter for correctness — each manager handles its
        own kind — but registration order determines log ordering on
        each tick, which may matter for debuggability.
        """
        self._managers.append(manager)

    @property
    def managers(self) -> list[InstanceLifecycleManager]:
        return list(self._managers)

    async def tick(self) -> dict[str, dict[str, int]]:
        """Run one reconciliation pass over all registered managers.

        For each registered manager:
          1. Compute the set of acceptable versions.
          2. Enumerate live instances.
          3. For each instance failing ``is_healthy``, call ``delete``
             with grace=0 — the crash-recovery path. Closes the gap
             where ``Unknown``/``Failed`` workspace pods sat forever
             (``docs/issues/stuck_thread_workspace_pods.md``).
          4. For each drifted instance that is currently idle, call
             ``drain``. Drift on a busy instance is recorded in stats
             but not actuated — the in-pod drain-intent path (Phase 1c)
             handles busy agents at their next safe boundary.
          5. Respect the disruption budget: skip drains for a kind once
             the per-tick cap is hit (only applies to drift drains;
             unhealthy deletes always proceed since they free capacity
             rather than consume it).

        Returns a per-kind stats dict for observability/tests:
        ``{"agent": {"listed": N, "unhealthy": N, "drift": N,
                     "drained": N, "skipped_busy": N}}``.
        """
        report: dict[str, dict[str, int]] = {}
        for manager in self._managers:
            kind = manager.kind
            stats = {
                "listed": 0,
                "unhealthy": 0,
                "drift": 0,
                "drained": 0,
                "skipped_busy": 0,
            }
            try:
                expected = await manager.expected_versions()
                instances = await manager.list_instances()
            except Exception:
                logger.exception("Lifecycle list failed for kind=%s", kind)
                report[kind] = stats
                continue
            stats["listed"] = len(instances)
            cap = self._budget.cap_for(kind, len(instances))
            drained = 0
            for inst in instances:
                # Crash recovery: unhealthy instances get force-deleted
                # before drift consideration. The manager decides what
                # 'unhealthy' means (pod phase, heartbeat freshness,
                # backend ping). Idempotent — delete on a missing pod
                # is a no-op.
                try:
                    healthy = await manager.is_healthy(inst)
                except Exception:
                    logger.exception(
                        "is_healthy raised for kind=%s id=%s — "
                        "treating as healthy to avoid mass-delete",
                        kind,
                        inst.id,
                    )
                    healthy = True
                if not healthy:
                    stats["unhealthy"] += 1
                    try:
                        await manager.delete(inst, grace_s=0)
                    except Exception:
                        logger.exception(
                            "Unhealthy-delete failed for kind=%s id=%s",
                            kind,
                            inst.id,
                        )
                    continue

                if not self.is_drift(inst, expected):
                    continue
                stats["drift"] += 1

                # Soft signal: write the drain-pending hint on every
                # drift, idle or busy. Cheap and idempotent. For agents
                # this is the trigger that lets a busy worker react at
                # its next phase boundary (Continue-as-New). For
                # workspaces and VMs this is a no-op — they get drained
                # the natural way once the bound work pauses.
                try:
                    await manager.signal_drain_pending(inst)
                except Exception:
                    logger.exception(
                        "signal_drain_pending failed for kind=%s id=%s",
                        kind,
                        inst.id,
                    )

                if not await manager.is_idle(inst):
                    stats["skipped_busy"] += 1
                    continue
                if drained >= cap or not self._budget.allow(kind):
                    continue
                try:
                    await manager.drain(inst, grace_s=0)
                    stats["drained"] += 1
                    drained += 1
                except Exception:
                    logger.exception(
                        "Drain failed for kind=%s id=%s",
                        kind,
                        inst.id,
                    )
            report[kind] = stats
            if any(v for k, v in stats.items() if k != "listed"):
                logger.info(
                    "Lifecycle tick kind=%s %s",
                    kind,
                    {k: v for k, v in stats.items() if v},
                )
        return report

    @staticmethod
    def is_stateful(manager: InstanceLifecycleManager) -> bool:
        """Type-narrowing helper for snapshot-capable managers.

        The reconciler will use this to gate snapshot calls before
        drain/delete on stateful instances (workspaces, VMs).
        """
        return isinstance(manager, StatefulInstanceManager)

    @staticmethod
    def is_drift(inst: Instance, expected: set[str]) -> bool:
        """Drift predicate: instance version is not in the expected set.

        Returns False when ``expected`` is empty (no SHA-pinned image —
        local dev) or when the instance has no recorded version yet
        (in flight). Drift detection is opt-in by configuration.
        """
        if not expected or inst.version is None:
            return False
        return inst.version not in expected
