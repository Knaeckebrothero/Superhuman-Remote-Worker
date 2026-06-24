"""``/api/projects/{project_id}/loop`` — start / inspect / control the project
self-improvement loop.

One active loop per project. **Start** spawns the first job (``role_sequence[0]``,
normally the scholar); the orchestrator's ``_advance_project_loop`` completion
hook drives the Scholar→Critic→Execution rotation from there, one job at a time,
until the iteration / deadline / consecutive-failure budget stops it.

Mirrors ``routers/automations.py`` conventions: handlers reach ``postgres_db``
(and the loop spawn helpers) via late ``from main import ...`` to dodge the
circular import at module load; ACL via ``require_project_member``.

Spec: docs/features/project_self_improvement_loop.md.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from security.access import require_project_member
from security.auth import require_approved_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/projects", tags=["Project Loops"])


class ProjectLoopStart(BaseModel):
    """Request body for ``POST /api/projects/{project_id}/loop``.

    At least one of ``max_iterations`` / ``run_until`` must be set — a loop can
    never be unbounded on both (enforced here and by a DB CHECK). ``goal`` is
    snapshotted from the project unless ``goal_override`` is given.
    """

    model: str | None = Field(None, max_length=200)
    role_sequence: list[str] | None = None
    max_iterations: int | None = Field(None, ge=1, le=1000)
    run_until: datetime | None = None
    acceptance_criteria: str | None = Field(None, max_length=8000)
    user_prompt: str | None = Field(None, max_length=8000)
    goal_override: str | None = Field(None, max_length=8000)
    max_consecutive_failures: int = Field(3, ge=1, le=50)


@router.post("/{project_id}/loop", status_code=status.HTTP_201_CREATED)
async def start_project_loop(
    request: Request, project_id: str, body: ProjectLoopStart
) -> dict[str, Any]:
    """Start a self-improvement loop on a project and spawn its first job.

    Editor or higher required. Rejects if the project already has an active
    (running|paused) loop. The first job is the first role in ``role_sequence``;
    everything after is driven by the completion hook.
    """
    from main import _spawn_loop_job, postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)
    await require_project_member(request, postgres_db, project_id, min_role="editor")

    # Budget: at least one stop axis must be set (hard floor under runaway).
    if body.max_iterations is None and body.run_until is None:
        raise HTTPException(
            status_code=400,
            detail="Set max_iterations and/or run_until — a loop cannot be "
            "unbounded on both.",
        )

    run_until = body.run_until
    if run_until is not None:
        if run_until.tzinfo is None:
            run_until = run_until.replace(tzinfo=timezone.utc)
        if run_until <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=400, detail="run_until must be in the future."
            )

    roles = body.role_sequence or ["scholar", "critic", "developer"]
    if not roles or not all(isinstance(r, str) and r.strip() for r in roles):
        raise HTTPException(
            status_code=400,
            detail="role_sequence must be a non-empty list of expert config names.",
        )

    if await postgres_db.get_active_project_loop(project_id):
        raise HTTPException(
            status_code=409,
            detail="Project already has an active loop. Stop it before starting another.",
        )

    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    goal = body.goal_override if body.goal_override is not None else project.get("goal")

    loop = await postgres_db.create_project_loop(
        project_id=project_id,
        owner_id=str(caller["id"]),
        goal=goal,
        acceptance_criteria=body.acceptance_criteria,
        user_prompt=body.user_prompt,
        model=body.model,
        role_sequence=roles,
        max_iterations=body.max_iterations,
        run_until=run_until,
        max_consecutive_failures=body.max_consecutive_failures,
    )

    # Spawn the first job and point the loop at it. If the first spawn fails,
    # mark the loop failed and surface a 502 — don't leave a running loop with
    # no in-flight job (the advance hook would never fire).
    try:
        first = await _spawn_loop_job(loop, role=roles[0], iteration=1)
    except Exception as e:
        logger.exception("Failed to spawn first job for loop %s", loop["id"])
        await postgres_db.update_project_loop(
            str(loop["id"]),
            status="failed",
            last_error=f"first spawn failed: {e}",
            stop_reason="failures",
        )
        raise HTTPException(
            status_code=502, detail=f"Loop created but first job failed to start: {e}"
        ) from e

    updated = await postgres_db.update_project_loop(
        str(loop["id"]),
        current_job_id=str(first["id"]),
        total_jobs_run=1,
    )
    return updated or loop


@router.get("/{project_id}/loop")
async def get_project_loop(request: Request, project_id: str) -> dict[str, Any]:
    """Return the project's active loop. 404 if none is active."""
    from main import postgres_db  # late import: avoid circular

    await require_approved_user(request, postgres_db)
    await require_project_member(request, postgres_db, project_id, min_role="viewer")
    loop = await postgres_db.get_active_project_loop(project_id)
    if not loop:
        raise HTTPException(status_code=404, detail="No active loop for this project")
    return loop


@router.post("/{project_id}/loop/pause")
async def pause_project_loop(request: Request, project_id: str) -> dict[str, Any]:
    """Pause the loop: the in-flight job finishes, but no next job is spawned."""
    from main import postgres_db  # late import: avoid circular

    await require_approved_user(request, postgres_db)
    await require_project_member(request, postgres_db, project_id, min_role="editor")
    loop = await postgres_db.get_active_project_loop(project_id)
    if not loop:
        raise HTTPException(status_code=404, detail="No active loop for this project")
    if loop["status"] != "running":
        return loop
    return await postgres_db.update_project_loop(str(loop["id"]), status="paused")


@router.post("/{project_id}/loop/resume")
async def resume_project_loop(request: Request, project_id: str) -> dict[str, Any]:
    """Resume a paused loop, re-kicking the rotation if its job already finished."""
    from main import _resume_project_loop, postgres_db  # late import: avoid circular

    await require_approved_user(request, postgres_db)
    await require_project_member(request, postgres_db, project_id, min_role="editor")
    loop = await postgres_db.get_active_project_loop(project_id)
    if not loop:
        raise HTTPException(status_code=404, detail="No active loop for this project")
    if loop["status"] != "paused":
        return loop
    resumed = await _resume_project_loop(str(loop["id"]))
    return resumed or loop


@router.post("/{project_id}/loop/stop")
async def stop_project_loop(request: Request, project_id: str) -> dict[str, Any]:
    """Stop the loop permanently. The in-flight job finishes on its own; the
    advance hook is a no-op once status is terminal.
    """
    from main import postgres_db  # late import: avoid circular

    await require_approved_user(request, postgres_db)
    await require_project_member(request, postgres_db, project_id, min_role="editor")
    loop = await postgres_db.get_active_project_loop(project_id)
    if not loop:
        raise HTTPException(status_code=404, detail="No active loop for this project")
    return await postgres_db.update_project_loop(
        str(loop["id"]), status="stopped", stop_reason="user", current_job_id=None
    )


@router.get("/{project_id}/loop/jobs")
async def list_project_loop_jobs(
    request: Request,
    project_id: str,
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List the active loop's spawned jobs, newest first."""
    from main import postgres_db  # late import: avoid circular

    await require_approved_user(request, postgres_db)
    await require_project_member(request, postgres_db, project_id, min_role="viewer")
    loop = await postgres_db.get_active_project_loop(project_id)
    if not loop:
        raise HTTPException(status_code=404, detail="No active loop for this project")
    return await postgres_db.list_project_loop_jobs(str(loop["id"]), limit=limit)
