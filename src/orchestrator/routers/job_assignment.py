"""HTTP adapter for the administrative job-assignment override.

Extracted verbatim from ``orchestrator.main`` (R1.B05 lane J, census group
``J_assignment``). One route: ``POST /api/jobs/{job_id}/assign/{agent_id}``.

The endpoint is admin-only (P4c) and is *not* the normal path — automatic
dispatch is. Its job is to refuse cleanly rather than push an incomplete bundle
at an agent: a stateless job cannot be assigned to a registered agent at all, a
job whose Kubernetes workspace authority has not converged answers 409 without
reserving anything, and a job with no live workspace sheds the stale workspace
context and hands itself back to the dispatcher instead of dispatching an SSH
config that names a pod which no longer exists.

The completion-control claim around the workspace-resume queueing is a pair:
every failure path after a successful claim aborts it, so a refused queueing
never leaves a job holding a control claim it does not own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request

from orchestrator.services.dispatch_guards import resume_lane_applies
from orchestrator.services.job_workspace_runtime import WORKSPACE_CONTEXT_KEYS
from shared.workspace_contract import resolve_workspace_runtime

router = APIRouter()


class JobAssignmentStore(Protocol):
    async def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None: ...

    async def job_has_checkpoint(self, job_id: str) -> bool: ...

    async def claim_job_for_agent(
        self, job_id: str, agent_id: str, **kwargs: Any
    ) -> Any: ...

    async def shed_workspace_context(self, job_id: str, context_key: str) -> Any: ...

    async def queue_job_for_resume(self, job_id: str, **kwargs: Any) -> Any: ...

    async def prepare_pinned_job_for_workspace_resume(
        self, job_id: str, context_key: str, **kwargs: Any
    ) -> Any: ...


@dataclass(frozen=True)
class JobAssignmentDependencies:
    """Per-invocation collaborators for the manual assignment override.

    ``completion_commands_enabled`` is a callable rather than the module flag's
    value so a monkeypatched flag on the application still steers this route
    (port contract §P1).
    """

    store: JobAssignmentStore
    logger: logging.Logger
    require_admin: Callable[[Request], Awaitable[Any]]
    vm_mode: Callable[[], Any]
    completion_commands_enabled: Callable[[], bool]

    # Lane J preparation seams the application owns.
    prepare_job_workspace_runtime: Callable[
        [dict[str, Any]], Awaitable[tuple[str, dict[str, Any], str | None]]
    ]
    prepare_job_repository_before_claim: Callable[[dict[str, Any]], Awaitable[bool]]
    resume_missing_workspace: Callable[[dict[str, Any]], str | None]

    # B08 completion control.
    guard_completion_control: Callable[..., Awaitable[None]]
    claim_completion_control: Callable[..., Awaitable[Any]]
    abort_completion_control_claim: Callable[[Any], Awaitable[None]]
    completion_resume_guard_kwargs: Callable[[], dict[str, Any]]

    # B09 delivery.
    dispatch_job_to_agent: Callable[[dict, dict], Awaitable[bool]]
    resume_job_on_agent: Callable[[dict, dict], Awaitable[bool]]

    # B11 scheduler.
    trigger_dispatch: Callable[[], None]


def get_job_assignment_dependencies(request: Request) -> JobAssignmentDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.job_assignment_dependencies_factory()


@router.post("/api/jobs/{job_id}/assign/{agent_id}")
async def assign_job_to_agent(
    request: Request,
    job_id: str,
    agent_id: str,
    *,
    dependencies: JobAssignmentDependencies = Depends(get_job_assignment_dependencies),
) -> dict[str, str]:
    """Administrative scheduling override for a job.

    **Admin only** (P4c). Normal callers should rely on automatic dispatch.
    If the managed workspace is not live, this endpoint sheds stale workspace
    state and queues normal provisioning instead of dispatching an incomplete
    SSH configuration. The requested agent is not reserved in that case.

    With a live workspace, validates the agent and delegates to the shared
    start/resume helper. Accepts 'created', 'failed', or 'paused' jobs.
    """
    postgres_db = dependencies.store
    logger = dependencies.logger

    await dependencies.require_admin(request)
    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job.get("execution_lane", "pinned") != "pinned":
            raise HTTPException(
                status_code=409,
                detail=(
                    "Stateless jobs are claimed from the run queue and cannot "
                    "be assigned directly to a registered agent"
                ),
            )

        if job["status"] not in ("created", "failed", "paused"):
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be assigned (status: {job['status']})",
            )

        await dependencies.guard_completion_control(job_id, source="manual_assign")

        (
            workspace_action,
            job,
            workspace_reason,
        ) = await dependencies.prepare_job_workspace_runtime(job)
        if workspace_action != "proceed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "workspace_runtime_adoption_pending",
                    "message": (
                        "Live Kubernetes workspace authority is not yet available; "
                        "no agent was reserved"
                    ),
                    "retryable": workspace_action == "wait",
                    "failure": workspace_reason,
                },
            )

        workspace_decision = resolve_workspace_runtime(
            job, vm_mode=dependencies.vm_mode()
        )
        if workspace_decision.contract is None or workspace_decision.state == "invalid":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "workspace_contract_invalid",
                    "message": (
                        "Job workspace authority is ambiguous; no agent was reserved"
                    ),
                    "state": workspace_decision.state,
                    "failure": workspace_decision.reason,
                },
            )

        missing_workspace = dependencies.resume_missing_workspace(job)
        if missing_workspace:
            if dependencies.completion_commands_enabled():
                control_claim = await dependencies.claim_completion_control(
                    {**job, "id": job_id}, source="manual_assign_workspace"
                )
                try:
                    queued = await postgres_db.prepare_pinned_job_for_workspace_resume(
                        job_id,
                        WORKSPACE_CONTEXT_KEYS[missing_workspace],
                        expected_status=str(job["status"]),
                        completion_control_claim_id=str(control_claim.claim_id),
                    )
                except Exception:
                    await dependencies.abort_completion_control_claim(control_claim)
                    raise
                if not queued:
                    await dependencies.abort_completion_control_claim(control_claim)
                    raise HTTPException(
                        status_code=409,
                        detail="Job changed while it was being queued for provisioning",
                    )
            else:
                await postgres_db.shed_workspace_context(
                    job_id, WORKSPACE_CONTEXT_KEYS[missing_workspace]
                )
            if (
                job["status"] != "created"
                and not dependencies.completion_commands_enabled()
            ):
                queued = await postgres_db.queue_job_for_resume(
                    job_id,
                    **dependencies.completion_resume_guard_kwargs(),
                )
                if not queued:
                    raise HTTPException(
                        status_code=409,
                        detail="Job changed while it was being queued for provisioning",
                    )
            dependencies.trigger_dispatch()
            return {
                "status": "queued",
                "job_id": job_id,
                "message": (
                    f"No live {missing_workspace} workspace; queued for automatic "
                    "provisioning and assignment. The requested agent was not reserved."
                ),
            }

        agent = await postgres_db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

        if agent["status"] != "ready":
            raise HTTPException(
                status_code=400,
                detail=f"Agent is not ready (status: {agent['status']})",
            )

        if not agent.get("pod_ip"):
            raise HTTPException(
                status_code=400,
                detail="Agent has no pod IP configured",
            )

        if not await dependencies.prepare_job_repository_before_claim(job):
            raise HTTPException(
                status_code=409,
                detail="Job repository authority is not ready",
            )
        if not await postgres_db.claim_job_for_agent(
            job_id,
            agent_id,
            completion_commands_enabled=dependencies.completion_commands_enabled(),
            allow_failed=True,
        ):
            raise HTTPException(
                status_code=409,
                detail="Job changed while it was being assigned",
            )

        # Use resume path for paused jobs that actually ran, start path for
        # new/failed — and for paused-but-never-started jobs, whose resume
        # would skip task-brief seeding (fresh_job_dispatched_as_resume_
        # skips_seeding.md).
        if resume_lane_applies(
            job, has_checkpoint=await postgres_db.job_has_checkpoint(job_id)
        ):
            success = await dependencies.resume_job_on_agent(job, agent)
        else:
            if job["status"] == "paused":
                logger.info(
                    "Assign: job %s is paused with no checkpoint to resume "
                    "from (never started, or pruned at a terminal state) — "
                    "dispatching via the fresh /job/start lane",
                    job_id,
                )
            success = await dependencies.dispatch_job_to_agent(job, agent)

        if not success:
            raise HTTPException(
                status_code=502,
                detail="Failed to dispatch job to agent",
            )

        return {"status": "assigned", "agent_id": agent_id, "job_id": job_id}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
