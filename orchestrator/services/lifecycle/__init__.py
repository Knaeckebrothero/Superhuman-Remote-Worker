"""Unified instance lifecycle management.

See ``knowledge-base/knowledge/features/unified_instance_lifecycle.md`` for the design.

Phase 1a ships the type / Protocol surface and a reconciler skeleton.
The actual cutover (AgentInstanceManager replacing _drain_stale_image_agents,
list_pods startup reconciliation, finalizer-based cleanup) lands in
Phase 1b.
"""

from .agent_manager import (
    AgentInstanceManager,
    expected_agent_shas,
)
from .reconciler import (
    DisruptionBudget,
    InstanceLifecycleReconciler,
)
from .persistent_agent_manager import PersistentAgentInstanceManager
from .types import (
    Instance,
    InstanceLifecycleManager,
    ReapableInstanceManager,
    StatefulInstanceManager,
)
from .vm_manager import (
    VMInstanceManager,
    expected_vm_shas,
)
from .workspace_manager import (
    WorkspaceInstanceManager,
    expected_workspace_shas,
)

__all__ = [
    "AgentInstanceManager",
    "DisruptionBudget",
    "Instance",
    "InstanceLifecycleManager",
    "InstanceLifecycleReconciler",
    "PersistentAgentInstanceManager",
    "ReapableInstanceManager",
    "StatefulInstanceManager",
    "VMInstanceManager",
    "WorkspaceInstanceManager",
    "expected_agent_shas",
    "expected_vm_shas",
    "expected_workspace_shas",
]
