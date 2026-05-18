"""Job-creation helper for automations.

Translates an automation row (one of the templates stored in the
``automations`` table) into an actual job by calling ``db.create_job()``
— the same DB write that the cockpit's ``POST /api/jobs`` handler issues.
Once the job lands in the table the existing dispatcher picks it up like
any other job; nothing in the agent, workspace, or job-detail view needs
to know the job came from an automation.

The reverse link is ``jobs.context->>'automation_id'``, set here on the
new job. ``automations.last_job_id`` is the forward link, written by
``db.advance_automation_after_fire`` in the cron dispatcher.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def create_job_from_automation(
    db: Any,
    automation: dict[str, Any],
    *,
    trigger_kind: str = "cron",
) -> dict[str, Any]:
    """Materialize a job from an automation template.

    Copies ``expert`` → ``config_name``, ``prompt`` → ``description``,
    and stamps ``context.automation_id`` / ``context.automation_name``
    so the cockpit "Runs" view (``list_automation_runs``) can join back.

    ``autonomy`` is injected into ``config_override`` because the jobs
    schema has no top-level autonomy column — dispatch reads it from
    ``config_override['autonomy']`` (orchestrator/main.py:933-935).

    Args:
        db: PostgresDB instance.
        automation: A row from ``automations`` (the dict returned by
            ``db.fetch_next_due_cron_automation`` or ``db.get_automation``).
        trigger_kind: "cron" today; v0.5 will pass "event" so the context
            tag can disambiguate. Stored as ``context.automation_trigger``.

    Returns:
        The created job dict (as ``db.create_job`` returns it), with at
        minimum ``id``, ``status``, and the template-derived fields.
    """
    # config_override starts as a copy of the automation's template; we
    # then layer the autonomy choice on top. Templates that explicitly
    # set autonomy via config_override take precedence (callers can
    # override per-automation defaults that way).
    config_override = dict(automation.get("config_override") or {})
    if "autonomy" not in config_override and automation.get("autonomy"):
        config_override["autonomy"] = automation["autonomy"]

    # Context tags drive the run-history join and give downstream code
    # (audit log, cockpit job-detail badge) the breadcrumb back to the
    # automation that fired the job.
    automation_id = str(automation["id"])
    context = {
        "automation_id": automation_id,
        "automation_name": automation.get("name") or "",
        "automation_trigger": trigger_kind,
    }

    project_id = automation.get("project_id")
    job = await db.create_job(
        description=automation["prompt"],
        config_name=automation["expert"],
        config_override=config_override,
        context=context,
        user_id=str(automation["owner_id"]),
        project_id=str(project_id) if project_id else None,
        priority=int(automation.get("priority", 5)),
    )

    logger.info(
        "Automation %s fired (%s) → job %s",
        automation_id,
        trigger_kind,
        job.get("id"),
    )
    return job
