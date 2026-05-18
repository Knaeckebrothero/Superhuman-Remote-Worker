"""``/api/automations`` — CRUD + run-now / pause / resume / runs.

First ``APIRouter`` in the project. Pattern: handlers reach for
``postgres_db`` via late import (``from main import postgres_db``)
inside each handler body to dodge circular import at module load time —
same pattern ``orchestrator/auth/bff.py`` established. ``_trigger_dispatch``
is similarly late-imported by the few handlers that create jobs (run-now)
so the new job is picked up by the auto-assign loop without waiting for
its 30s tick.

Spec: docs/features/automations_v0.md §Endpoints.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from security.access import require_project_member
from security.auth import require_approved_user
from services.automations import create_job_from_automation
from services.cron_dispatcher import (
    compute_initial_next_run,
    validate_cron_expr,
    validate_timezone,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/automations", tags=["Automations"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AutomationCreate(BaseModel):
    """Request body for ``POST /api/automations``.

    v0 only accepts cron triggers; ``trigger_type`` is omitted from the
    public surface and forced server-side. Event triggers ship in v0.5.
    """

    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    cron_expr: str = Field(..., min_length=1, max_length=200)
    timezone: str = Field("UTC", max_length=64)
    catchup_window_seconds: int = Field(86400, ge=0, le=7 * 86400)

    expert: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1)
    config_override: dict[str, Any] | None = None
    autonomy: str = Field("review", pattern=r"^(full|review|partial|guided|dependent)$")
    priority: int = Field(5, ge=0, le=10)

    enabled: bool = True
    max_chain_depth: int = Field(10, ge=1, le=100)
    max_fires_per_day: int = Field(100, ge=1, le=10_000)

    project_id: str | None = None


class AutomationUpdate(BaseModel):
    """Request body for ``PATCH /api/automations/{id}``.

    All fields optional. Server recomputes ``next_run_at`` when
    ``cron_expr`` / ``timezone`` / ``enabled`` changes so a pause-then-resume
    or a cron edit lands atomically.
    """

    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)
    cron_expr: str | None = Field(None, min_length=1, max_length=200)
    timezone: str | None = Field(None, max_length=64)
    catchup_window_seconds: int | None = Field(None, ge=0, le=7 * 86400)
    expert: str | None = Field(None, min_length=1, max_length=120)
    prompt: str | None = Field(None, min_length=1)
    config_override: dict[str, Any] | None = None
    autonomy: str | None = Field(
        None, pattern=r"^(full|review|partial|guided|dependent)$"
    )
    priority: int | None = Field(None, ge=0, le=10)
    enabled: bool | None = None
    max_chain_depth: int | None = Field(None, ge=1, le=100)
    max_fires_per_day: int | None = Field(None, ge=1, le=10_000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_automation_or_404(
    db: Any,
    automation_id: str,
    caller: dict[str, Any],
    request: Request,
    *,
    min_project_role: str = "viewer",
) -> dict[str, Any]:
    """Fetch an automation and enforce ACL.

    Visibility rules: caller is the owner, OR caller is a project member
    (at ``min_project_role`` or higher) of the automation's project, OR
    caller is admin. Raises 404 if the row doesn't exist (don't leak
    existence to non-members) and 403 on access denied.
    """
    row = await db.get_automation(automation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Automation not found")

    if caller.get("is_admin"):
        return row
    if str(row["owner_id"]) == str(caller["id"]):
        return row
    if row.get("project_id"):
        # require_project_member raises 403/404 on its own
        await require_project_member(
            request, db, str(row["project_id"]), min_role=min_project_role
        )
        return row
    raise HTTPException(status_code=404, detail="Automation not found")


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_automation(request: Request, body: AutomationCreate) -> dict[str, Any]:
    """Create a new automation (cron-only in v0).

    The caller becomes ``owner_id``. If ``project_id`` is set the caller
    must be at least an editor of that project. ``next_run_at`` is seeded
    here from the cron expression so the first dispatcher tick after
    create can fire it without an extra round-trip.
    """
    from main import postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)

    # Boundary validation — surface bad cron / tz as 400s rather than 500s
    try:
        validate_cron_expr(body.cron_expr)
        validate_timezone(body.timezone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.project_id:
        # Editor or higher needed to scope an automation to a project —
        # an automation creates jobs that show up on the project page.
        await require_project_member(
            request, postgres_db, body.project_id, min_role="editor"
        )

    next_run = (
        compute_initial_next_run(body.cron_expr, body.timezone)
        if body.enabled
        else None
    )

    row = await postgres_db.create_automation(
        owner_id=str(caller["id"]),
        project_id=body.project_id,
        name=body.name,
        description=body.description,
        trigger_type="cron",
        cron_expr=body.cron_expr,
        timezone=body.timezone,
        catchup_window_seconds=body.catchup_window_seconds,
        enabled=body.enabled,
        expert=body.expert,
        prompt=body.prompt,
        config_override=body.config_override or {},
        autonomy=body.autonomy,
        priority=body.priority,
        max_chain_depth=body.max_chain_depth,
        max_fires_per_day=body.max_fires_per_day,
        next_run_at=next_run,
    )
    return row


@router.get("")
async def list_automations(
    request: Request,
    project_id: str | None = Query(None, description="Filter by project"),
) -> list[dict[str, Any]]:
    """List automations visible to the caller.

    Default scope = caller's own automations (admins included — there is
    no "see everyone's automations" view in v0; use a per-user view-as
    toggle to inspect another user's). With ``project_id`` set, returns
    all automations on that project (across owners) provided the caller
    is a project member; otherwise 403.
    """
    from main import postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)

    if project_id is not None:
        # Membership check enforces visibility for cross-owner project view.
        await require_project_member(
            request, postgres_db, project_id, min_role="viewer"
        )
        return await postgres_db.list_automations(project_id=project_id)

    return await postgres_db.list_automations(owner_id=str(caller["id"]))


@router.get("/{automation_id}")
async def get_automation(request: Request, automation_id: str) -> dict[str, Any]:
    """Fetch a single automation. 404 if not visible to the caller."""
    from main import postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)
    return await _resolve_automation_or_404(postgres_db, automation_id, caller, request)


@router.patch("/{automation_id}")
async def update_automation(
    request: Request, automation_id: str, body: AutomationUpdate
) -> dict[str, Any]:
    """Partial update. Recomputes ``next_run_at`` when cron / tz / enabled
    changes; pause (enabled=false) clears it.
    """
    from main import postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)
    row = await _resolve_automation_or_404(
        postgres_db, automation_id, caller, request, min_project_role="editor"
    )

    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return row

    if "cron_expr" in fields:
        try:
            validate_cron_expr(fields["cron_expr"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "timezone" in fields:
        try:
            validate_timezone(fields["timezone"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Recompute next_run_at when the schedule shape or enable state changes.
    # Pull the effective post-patch values from `fields` falling back to row.
    schedule_changed = bool({"cron_expr", "timezone", "enabled"} & set(fields))
    if schedule_changed:
        enabled_after = fields.get("enabled", row["enabled"])
        if enabled_after:
            cron_after = fields.get("cron_expr", row["cron_expr"])
            tz_after = fields.get("timezone", row["timezone"])
            fields["next_run_at"] = compute_initial_next_run(cron_after, tz_after)
        else:
            fields["next_run_at"] = None

    updated = await postgres_db.update_automation(automation_id, **fields)
    return updated or row


@router.delete("/{automation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_automation(request: Request, automation_id: str) -> None:
    """Hard-delete. Past spawned jobs survive — the back-link in
    ``jobs.context['automation_id']`` becomes orphaned, intentional.
    """
    from main import postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)
    await _resolve_automation_or_404(
        postgres_db, automation_id, caller, request, min_project_role="editor"
    )
    await postgres_db.delete_automation(automation_id)


# ---------------------------------------------------------------------------
# Action endpoints
# ---------------------------------------------------------------------------


@router.post("/{automation_id}/run-now")
async def run_now(request: Request, automation_id: str) -> dict[str, Any]:
    """Fire the automation immediately, regardless of schedule.

    Does NOT touch ``next_run_at`` — the next scheduled fire still happens
    on time. Useful for "test this thing I just edited" and for the
    cockpit's Run-now button.
    """
    from main import _trigger_dispatch, postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)
    row = await _resolve_automation_or_404(
        postgres_db, automation_id, caller, request, min_project_role="editor"
    )

    job = await create_job_from_automation(postgres_db, row, trigger_kind="manual")

    # Best-effort nudge to the auto-assign dispatcher so the new job
    # doesn't sit idle for up to 30s waiting on the scheduled tick.
    try:
        _trigger_dispatch()
    except Exception:
        logger.exception("run-now: _trigger_dispatch raised (non-fatal)")

    return {"automation_id": automation_id, "job": job}


@router.post("/{automation_id}/pause")
async def pause_automation(request: Request, automation_id: str) -> dict[str, Any]:
    """Set ``enabled=false`` and clear ``next_run_at`` so the dispatcher
    stops considering this automation. Reversible via ``resume``.
    """
    from main import postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)
    await _resolve_automation_or_404(
        postgres_db, automation_id, caller, request, min_project_role="editor"
    )
    return await postgres_db.update_automation(
        automation_id,
        enabled=False,
        next_run_at=None,
    )


@router.post("/{automation_id}/resume")
async def resume_automation(request: Request, automation_id: str) -> dict[str, Any]:
    """Set ``enabled=true`` and recompute ``next_run_at`` from now."""
    from main import postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)
    row = await _resolve_automation_or_404(
        postgres_db, automation_id, caller, request, min_project_role="editor"
    )
    next_run = compute_initial_next_run(row["cron_expr"], row["timezone"])
    return await postgres_db.update_automation(
        automation_id,
        enabled=True,
        next_run_at=next_run,
    )


@router.get("/{automation_id}/runs")
async def list_runs(
    request: Request,
    automation_id: str,
    limit: int = Query(50, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List jobs spawned by this automation, newest first.

    Joins on ``jobs.context->>'automation_id'`` which the dispatcher
    writes at fire time (``services/automations.py``).
    """
    from main import postgres_db  # late import: avoid circular

    caller = await require_approved_user(request, postgres_db)
    await _resolve_automation_or_404(postgres_db, automation_id, caller, request)
    return await postgres_db.list_automation_runs(automation_id, limit=limit)
