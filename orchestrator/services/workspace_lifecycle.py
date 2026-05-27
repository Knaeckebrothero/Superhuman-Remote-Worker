"""Owner-keyed workspace lifecycle: one provisioning path for jobs and sessions.

See docs/features/unified_workspace_provisioning.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OwnerKind = Literal["job", "session"]


@dataclass(frozen=True)
class WorkspaceOwner:
    """Identifies whose workspace this is. Collapses the job/thread split."""

    kind: OwnerKind
    id: str

    @classmethod
    def job(cls, job_id: str) -> "WorkspaceOwner":
        return cls("job", job_id)

    @classmethod
    def session(cls, thread_id: str) -> "WorkspaceOwner":
        return cls("session", thread_id)

    @property
    def pod_name(self) -> str:
        prefix = "workspace" if self.kind == "job" else "ws-thread"
        return f"{prefix}-{self.id[:12]}"

    @property
    def label_key(self) -> str:
        return "srw/job-id" if self.kind == "job" else "srw/thread-id"

    @property
    def component_label(self) -> str:
        return "workspace" if self.kind == "job" else "thread-workspace"

    @property
    def network_tier_kind(self) -> str:
        # Arg expected by ContainerProvisioner._resolve_network_tier / DB.
        return "job" if self.kind == "job" else "thread"
