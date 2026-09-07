"""Workspace provisioning request contracts."""

from pydantic import BaseModel, Field


class VMCreateRequest(BaseModel):
    """Request body for creating a VM for a job."""

    job_id: str
    agent_config: str = "worker_base"
    vm_image: str | None = None
    cpu_cores: int = Field(8, ge=1, le=16)
    memory: str = "16Gi"
    description: str = ""
