"""Filing a campaign plan from a planner loop's checkpoint critic.

Extracted verbatim from ``orchestrator.main`` (R1.B07 lane L, census group
``J_project_loops``; loop_campaign_scheduling.md). One operation behind one
internal route.

Validation happens **at call time** so the critic learns about a malformed plan
while it can still fix it — that is the whole point of tool transport over
``freeze_data``. The gating is defence in depth with the P1 spawn-time tool
injection: only the loop's in-flight job may file, only on a running
campaign-scheduled loop, and only from the checkpoint-critic stage — a campaign
member occupies the execution slot, so it is rejected structurally rather than
by a role-string check.

The KB existence check for the initiative note is deliberately best effort: a
down or absent vector store accepts the plan (KB failures are non-fatal by
convention), while a present store that cannot find the note rejects it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from orchestrator.schemas.project_loops import LoopPlanRequest

logger = logging.getLogger(__name__)


@dataclass
class LoopPlanFilingDependencies:
    """The app and vector stores, resolved per invocation."""

    store: Any
    vector_store: Any


async def file_loop_plan(
    request: Request,
    job_id: str,
    body: LoopPlanRequest,
    *,
    dependencies: LoopPlanFilingDependencies,
) -> dict[str, Any]:
    """File a campaign plan from a planner loop's checkpoint critic. **Internal**
    (P4b) — requires ``X-Internal-Key``. Ingress strips this path.

    Validated at call time so the critic learns about a malformed plan while it
    can still fix it (the whole point of tool-transport over freeze_data). The
    normalized plan is stored in the job's context (``loop_plan``) via the
    atomic context merge; the loop's advance applies it when the critic job
    completes. Idempotent: re-filing replaces the stored plan.

    Gating (defense in depth with the P1 spawn-time tool injection): only the
    loop's in-flight job may file, only on a running planner-scheduled loop,
    and only from the checkpoint-critic stage — a campaign member occupies the
    execution slot, so it is rejected structurally, not by role-string check.
    """
    from orchestrator.services.project_loops import (
        job_loop_id,
        planner_slots,
        validate_loop_plan,
    )

    job = await dependencies.store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    ctx = job.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    ctx = ctx or {}

    loop_id = job_loop_id(job)
    if not loop_id:
        raise HTTPException(status_code=400, detail="Not a project-loop job")
    loop = await dependencies.store.get_project_loop(loop_id)
    if not loop:
        raise HTTPException(status_code=404, detail="Loop not found")
    if (loop.get("scheduling") or "standard") != "campaign":
        raise HTTPException(
            status_code=409,
            detail="This loop uses standard scheduling — plans are only "
            "accepted on campaign-scheduled loops",
        )
    if loop.get("status") != "running":
        raise HTTPException(
            status_code=409, detail=f"Loop is {loop.get('status')}, not running"
        )
    stage_ids = [str(x) for x in (loop.get("current_stage_jobs") or [])]
    if str(job["id"]) not in stage_ids:
        raise HTTPException(
            status_code=409,
            detail="Job is not one of the loop's in-flight jobs",
        )
    if ctx.get("loop_role") != "critic":
        raise HTTPException(
            status_code=403, detail="Only the checkpoint critic files plans"
        )
    try:
        critic_slot, _execution_slot = planner_slots(loop.get("role_sequence") or [])
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    stamped_seq = ctx.get("loop_seq_index")
    if stamped_seq is not None and int(stamped_seq) != critic_slot:
        raise HTTPException(
            status_code=403,
            detail="Campaign members cannot file plans — only the loop's "
            "checkpoint critic stage can (sub-critic rule)",
        )

    try:
        normalized = validate_loop_plan(body.plan, loop)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # The initiative must be a real KB note in the loop's project. Best-effort:
    # a down/absent vector store accepts the plan (KB failures are non-fatal by
    # convention) — a present store that can't find the note rejects it.
    project_id = loop.get("project_id")
    if (
        project_id
        and dependencies.vector_store is not None
        and normalized["initiative"] is not None
    ):
        note_id = normalized["initiative"]["kb_note_id"]
        try:
            async with dependencies.vector_store.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT 1 FROM knowledge_index "
                    "WHERE project_id = $1::uuid AND note_id = $2 LIMIT 1",
                    str(project_id),
                    note_id,
                )
        except Exception:
            logger.warning(
                "loop-plan: KB existence check unavailable — accepting plan "
                "for job %s without it",
                job_id,
                exc_info=True,
            )
            row = True
        if not row:
            raise HTTPException(
                status_code=400,
                detail=f"initiative note '{note_id}' not found in the project "
                "KB — kb_write it first, or fix the id",
            )

    if not await dependencies.store.merge_job_context(
        job_id, {"loop_plan": normalized}
    ):
        raise HTTPException(status_code=500, detail="Failed to store the plan")
    if normalized["initiative"] is None:
        logger.info(
            "project loop %s: critic job %s filed a dispose-only plan (%s)",
            str(loop_id)[:8],
            job_id[:8],
            normalized["disposition"]["outcome"],
        )
    else:
        logger.info(
            "project loop %s: critic job %s filed a %d-stage campaign plan (%s)",
            str(loop_id)[:8],
            job_id[:8],
            len(normalized["stages"]),
            normalized["initiative"]["kb_note_id"],
        )
    return {"status": "accepted", "plan": normalized}


__all__ = ["LoopPlanFilingDependencies", "file_loop_plan"]
