"""Resume the waiting curator with a final-pass signal.

Extracted verbatim from ``orchestrator.main`` (R1.B07 lane L, census group
``J_project_loops``). One operation, called after critic approval (or
auto-accept) when curation is enabled. It is skipped for a lite workspace
backend, which has no git workspace for the curator subjob handoff.

The command-aware branch matters: when the exact command already committed its
status/context handoff, the dispatcher is re-kicked and **no new stateless
resume generation is minted** for the same S31 — a marker-window crash must not
produce a second resume.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from orchestrator.services.workspace_tier_policy import is_lite_config_override

logger = logging.getLogger(__name__)


@dataclass
class CurationFinalPassDependencies:
    """Collaborators for one curation handoff, resolved per invocation."""

    store: Any
    trigger_dispatch: Callable[[], None]
    internal_resume_job: Callable[..., Awaitable[Any]]


async def trigger_curation_final_pass(
    target_job_id: str,
    target_job: dict[str, Any] | None = None,
    *,
    completion_command_id: str | None = None,
    dependencies: CurationFinalPassDependencies,
) -> None:
    """Resume the waiting curator with a final-pass signal.

    Called after critic approval (or auto-accept) when curation is enabled.
    """
    from orchestrator.services.completion import (
        get_curation_config,
        is_curation_enabled,
    )

    if target_job is None:
        target_job = await dependencies.store.get_job(target_job_id)
    if not target_job:
        return
    if is_lite_config_override(target_job.get("config_override")):
        logger.info(
            f"Curation skipped for job {target_job_id}: lite workspace backend "
            f"has no git workspace for the curator subjob handoff"
        )
        return
    if not is_curation_enabled(target_job):
        return

    curator_config_name = get_curation_config(target_job).get(
        "curator_config", "curator"
    )

    # Find a waiting curator for this target job
    async with dependencies.store.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, status FROM jobs
               WHERE parent_job_id = $1::uuid AND config_name = $2
               AND status IN ('waiting', 'paused')
               ORDER BY created_at DESC LIMIT 1""",
            target_job_id,
            curator_config_name,
        )

    if not row:
        logger.debug(f"No waiting curator found for job {target_job_id}")
        return

    if row["status"] == "completed":
        return

    curator_id = str(row["id"])
    if completion_command_id is not None:
        curator = await dependencies.store.get_job(curator_id)
        curator_context = (curator or {}).get("context") or {}
        if isinstance(curator_context, str):
            try:
                curator_context = json.loads(curator_context)
            except (TypeError, ValueError):
                curator_context = {}
        if (
            isinstance(curator_context, Mapping)
            and curator_context.get("curation_final_pass_completion_command_id")
            == completion_command_id
        ):
            # The exact command already committed its status/context handoff.
            # Re-kick the idempotent dispatcher after a marker-window crash;
            # never mint a new stateless resume generation for the same S31.
            dependencies.trigger_dispatch()
            return
    logger.info(
        f"Triggering curation final pass via curator {curator_id} for {target_job_id}"
    )
    queued = await dependencies.internal_resume_job(
        curator_id,
        feedback=(
            "FINAL CURATION PASS. The target job has been approved by the critic. "
            "Do a comprehensive final sweep: read memories, output/, and the final "
            "workspace.md. Promote valuable memories to knowledge notes. Write a "
            "`state` note summarizing what changed. Check for open questions. "
            "Link all notes. Then call job_complete."
        ),
        additional_context=(
            {"curation_final_pass_completion_command_id": completion_command_id}
            if completion_command_id is not None
            else None
        ),
    )
    if completion_command_id is not None and not queued:
        raise RuntimeError("curation final-pass handoff lost its queue CAS")


__all__ = ["CurationFinalPassDependencies", "trigger_curation_final_pass"]
