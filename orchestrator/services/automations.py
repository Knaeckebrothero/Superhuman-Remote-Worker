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
import os
from typing import Any

from .default_experts import resolve_root_expert
from src.core.loader import canonical_config_name, deep_merge

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

    # NOTE: with explicit-only datasource resolution, automation-fired jobs
    # attach no datasources (the automation row carries no selection and this
    # is a non-UI path with no parent to inherit from). Per-automation
    # datasource selection is a follow-up; see multi_datasource_support.md.
    project_id = automation.get("project_id")

    # Resolve the automation's expert NAME to a DB expert_id when DB-backed
    # experts are on (decision 5/15: name-resolving automations are live refs).
    # Falls through to config_name (bundled) when nothing matches.
    expert_id = None
    config_name = canonical_config_name(str(automation["expert"]))
    if os.getenv("EXPERTS_DB_ENABLED", "true").lower().strip() in (
        "true",
        "1",
        "yes",
    ):
        from src.core.expert_resolution import pick_expert_by_name

        owner_id = str(automation["owner_id"])
        pids = [str(project_id)] if project_id else []
        try:
            if config_name == "worker_base":
                owner = await db.get_user(owner_id)
                selection = await resolve_root_expert(
                    db,
                    expert_type="worker",
                    user_id=owner_id,
                    project_id=str(project_id) if project_id else None,
                    is_admin=bool((owner or {}).get("is_admin")),
                )
                expert_id = str(selection.expert["id"])
                context["expert_selection"] = {
                    "source": selection.source,
                    "expert_id": expert_id,
                }
                if selection.project_override:
                    # Project expert settings sit below the automation's own
                    # per-run override, matching the normal root-job path.
                    config_override = deep_merge(
                        selection.project_override, config_override
                    )
            else:
                candidates = await db.list_experts_visible(
                    user_id=owner_id, project_ids=pids, expert_type="worker"
                )
                matches = [c for c in candidates if c["name"] == automation["expert"]]
                winner = pick_expert_by_name(matches, owner_id, set(pids))
                if winner:
                    expert_id = str(winner["id"])
                    config_name = "worker_base"
        except Exception as e:
            if config_name == "worker_base":
                raise
            logger.warning(
                "Automation expert resolution failed (using config_name): %s", e
            )

    job = await db.create_job(
        description=automation["prompt"],
        config_name=config_name,
        config_override=config_override,
        context=context,
        user_id=str(automation["owner_id"]),
        project_id=str(project_id) if project_id else None,
        priority=int(automation.get("priority", 5)),
        expert_id=expert_id,
    )

    logger.info(
        "Automation %s fired (%s) → job %s",
        automation_id,
        trigger_kind,
        job.get("id"),
    )
    return job
