"""Pure logic functions for job completion handling.

Extracted from agent-side post-completion code (src/api/app.py) so the
orchestrator can make verification/curation decisions without depending
on agent state.  All functions read config from the job dict's
``resolved_config`` JSONB — no live agent config needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Repo root for template resolution
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Config helpers — read from resolved_config JSONB
# ---------------------------------------------------------------------------

def _parse_resolved_config(job: dict[str, Any]) -> dict[str, Any]:
    """Parse resolved_config from a job dict (handles str or dict)."""
    rc = job.get("resolved_config")
    if rc is None:
        return {}
    if isinstance(rc, str):
        try:
            return json.loads(rc)
        except (json.JSONDecodeError, ValueError):
            return {}
    return rc if isinstance(rc, dict) else {}


def get_verification_config(job: dict[str, Any]) -> dict[str, Any]:
    """Extract verification config from resolved_config.

    Checks ``resolved_config.agent.verification`` first (standard path after
    ``serialize_resolved_config`` flattens ``extra`` into the agent dict),
    then falls back to ``resolved_config.verification`` (direct path).
    """
    rc = _parse_resolved_config(job)

    # Primary path: resolved_config -> agent -> verification
    agent_block = rc.get("agent")
    if isinstance(agent_block, dict):
        vc = agent_block.get("verification")
        if isinstance(vc, dict):
            return vc

    # Fallback: top-level verification key
    vc = rc.get("verification")
    if isinstance(vc, dict):
        return vc

    return {}


def is_verification_enabled(job: dict[str, Any]) -> bool:
    """Check if verification is enabled for a job."""
    return bool(get_verification_config(job).get("enabled", False))


def get_curation_config(job: dict[str, Any]) -> dict[str, Any]:
    """Extract curation config from resolved_config.

    Same resolution pattern as ``get_verification_config`` but for the
    ``curator`` key.
    """
    rc = _parse_resolved_config(job)

    agent_block = rc.get("agent")
    if isinstance(agent_block, dict):
        cc = agent_block.get("curator")
        if isinstance(cc, dict):
            return cc

    cc = rc.get("curator")
    if isinstance(cc, dict):
        return cc

    return {}


def is_curation_enabled(job: dict[str, Any]) -> bool:
    """Check if curation is enabled for a job."""
    return bool(get_curation_config(job).get("enabled", False))


def get_autonomy_level(job: dict[str, Any]) -> str:
    """Read the autonomy level from resolved_config (default: 'review')."""
    rc = _parse_resolved_config(job)
    agent_block = rc.get("agent")
    if isinstance(agent_block, dict):
        return agent_block.get("autonomy", "review")
    return rc.get("autonomy", "review")


# ---------------------------------------------------------------------------
# Freeze data helpers
# ---------------------------------------------------------------------------

def _parse_freeze_data(job: dict[str, Any]) -> dict[str, Any] | None:
    """Parse freeze_data from a job dict (handles str or dict)."""
    fd = job.get("freeze_data")
    if fd is None:
        return None
    if isinstance(fd, str):
        try:
            return json.loads(fd)
        except (json.JSONDecodeError, ValueError):
            return None
    return fd if isinstance(fd, dict) else None


def is_job_completion_freeze(job: dict[str, Any]) -> bool:
    """Check if a job's freeze_data indicates job completion (not phase boundary).

    Two formats exist depending on autonomy level:
      - review/partial: ``freeze_type="job_complete"``
      - full: ``status="job_completed"`` (no freeze_type field)
    """
    freeze_data = _parse_freeze_data(job)
    if not freeze_data:
        return False
    freeze_type = freeze_data.get("freeze_type")
    return (
        freeze_type == "job_complete"
        or freeze_data.get("status") == "job_completed"
    )


# ---------------------------------------------------------------------------
# Status determination
# ---------------------------------------------------------------------------

def determine_job_status(
    job: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Determine the new job status from the graph execution result.

    Mirrors the decision tree in ``_update_job_status_from_result()``
    (src/api/app.py) but reads config from the DB rather than agent memory.

    Returns:
        ``(new_status, error_message)`` — either or both may be ``None``
        to indicate no change is needed for that field.
    """
    error = result.get("error")
    should_stop = result.get("should_stop", False)
    goal_achieved = result.get("goal_achieved", False)

    if error:
        error_msg = (
            error.get("message", str(error))
            if isinstance(error, dict)
            else str(error)
        )
        return ("failed", error_msg)

    if not should_stop:
        return (None, None)  # Still running — leave as processing

    # Critic jobs (have parent_job_id): graph.py / handle_transition already
    # set the correct status (e.g. 'waiting' for returned verdicts).
    if job.get("parent_job_id") is not None:
        logger.debug(
            "Job %s is a critic — skipping status override (handle_transition set it)",
            job.get("id"),
        )
        return (None, None)

    # Job completion (any autonomy level)
    if goal_achieved or is_job_completion_freeze(job):
        if is_verification_enabled(job):
            return ("reviewing", None)
        elif goal_achieved:
            # Full autonomy — graph.py already set 'completed'
            return (None, None)
        else:
            # Non-full autonomy, no verification — keep pending_review
            return ("pending_review", None)

    # Phase boundary freeze or other non-completion stop
    return ("pending_review", None)


# ---------------------------------------------------------------------------
# Template formatting
# ---------------------------------------------------------------------------

def format_verification_instructions(
    job_id: str,
    description: str,
    freeze_data: dict[str, Any],
    config_name: str,
) -> str | None:
    """Load and format the verification instructions template.

    Moved from ``OrchestratorClient._format_verification_instructions``.
    Template is loaded from ``config/experts/critic/verification_instructions.md``
    with fallback to ``config/templates/verification_instructions.md``.
    """
    search_paths = [
        _REPO_ROOT / "config" / "experts" / "critic" / "verification_instructions.md",
        _REPO_ROOT / "config" / "templates" / "verification_instructions.md",
    ]

    template_path = None
    for path in search_paths:
        if path.exists():
            template_path = path
            break

    if not template_path:
        logger.error(
            "Verification instructions template not found. Searched: %s",
            [str(p) for p in search_paths],
        )
        return None

    try:
        template = template_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed to read verification template %s: %s", template_path, e)
        return None

    # Format deliverables as a bulleted list
    deliverables = freeze_data.get("deliverables", [])
    if deliverables:
        deliverables_list = "\n".join(f"- `{d}`" for d in deliverables)
    else:
        deliverables_list = "- *(no deliverables listed)*"

    confidence = freeze_data.get("confidence", 0)
    confidence_str = (
        f"{confidence:.0%}" if isinstance(confidence, (int, float)) else str(confidence)
    )

    try:
        return template.format(
            target_job_id=job_id,
            target_config=config_name,
            target_description=description,
            deliverables_list=deliverables_list,
            agent_summary=freeze_data.get("summary", "*(no summary provided)*"),
            agent_confidence=confidence_str,
        )
    except KeyError as e:
        logger.error("Verification template has unknown placeholder: %s", e)
        return None


def format_curation_instructions(
    job_id: str,
    description: str,
    config_name: str,
    phase_data: str,
    curation_mode: str = "incremental",
    curation_phase: str = "initial",
) -> str | None:
    """Load and format the curation instructions template.

    Moved from ``OrchestratorClient._format_curation_instructions``.
    """
    search_paths = [
        _REPO_ROOT / "config" / "experts" / "curator" / "curation_instructions.md",
        _REPO_ROOT / "config" / "templates" / "curation_instructions.md",
    ]

    template_path = None
    for path in search_paths:
        if path.exists():
            template_path = path
            break

    if not template_path:
        logger.error(
            "Curation instructions template not found. Searched: %s",
            [str(p) for p in search_paths],
        )
        return None

    try:
        template = template_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed to read curation template %s: %s", template_path, e)
        return None

    if curation_mode == "incremental":
        task_instructions = (
            "Read the phase artifacts below and extract knowledge notes. "
            "Search existing knowledge with `kb_search` before writing to avoid duplicates. "
            "Write notes via `kb_write`, then call `job_complete` with a summary."
        )
    else:
        task_instructions = (
            "This is the FINAL curation pass. Read memories, output/, and the final workspace.md. "
            "Promote valuable memories to knowledge notes. Write a `state` note summarizing "
            "what changed in the project. Check for open questions and unresolved items. "
            "Link all notes to related existing knowledge. Call `job_complete` when done."
        )

    try:
        return template.format(
            target_job_id=job_id,
            target_config=config_name,
            target_description=description,
            curation_phase=curation_phase,
            curation_mode=curation_mode,
            phase_context=phase_data,
            task_instructions=task_instructions,
        )
    except KeyError as e:
        logger.error("Curation template has unknown placeholder: %s", e)
        return None
