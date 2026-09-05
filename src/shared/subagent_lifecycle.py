"""Typed parent-lifecycle failures for durable subagent runtimes.

This module intentionally lives below ``agent.subagents``.  Agent and executor
drivers import it at module load time, while the subagent package itself stays
lazy to avoid the registry/delegation/persistent-graph import cycle.
"""

from __future__ import annotations


class SubagentLifecycleError(RuntimeError):
    """A parent could not safely settle its claim-local child runtime."""


class SubagentRecoveryError(SubagentLifecycleError):
    """Durable predecessor child generations could not be reconciled."""


class SubagentQuiescenceError(SubagentLifecycleError):
    """Current child work could not be terminally joined and persisted."""


class SubagentAbandonError(SubagentLifecycleError):
    """Authority-loss cleanup could not prove all local child work stopped."""


__all__ = [
    "SubagentAbandonError",
    "SubagentLifecycleError",
    "SubagentQuiescenceError",
    "SubagentRecoveryError",
]
