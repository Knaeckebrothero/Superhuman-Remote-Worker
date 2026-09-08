"""Request contracts for the IDE session and workspace-upgrade surfaces.

``JobWorkspaceUpgradeRequest`` is declared here rather than in
:mod:`orchestrator.schemas.workspaces` because a FastAPI body annotation has to
resolve at route-declaration time — it cannot be handed to the router through
the per-request dependency factory the way a collaborator object can. Its only
consumer is ``POST /api/jobs/{job_id}/provision-workspace`` in
:mod:`orchestrator.routers.workspace_access`.
"""

from pydantic import BaseModel, Field


class IdeSessionRequest(BaseModel):
    """Request body for starting an IDE session."""

    cpu_cores: int = Field(8, description="VM CPU cores")
    memory: str = Field("16Gi", description="VM memory")
    idle_timeout_minutes: int | None = Field(
        None, description="Override default idle timeout"
    )


class JobWorkspaceUpgradeRequest(BaseModel):
    target_tier: str = "sandbox"
