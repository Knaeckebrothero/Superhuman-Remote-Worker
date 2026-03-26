"""Pure logic functions for job completion handling.

Extracted from agent-side post-completion code (src/api/app.py) so the
orchestrator can make verification/curation decisions without depending
on agent state.  All functions read config from the job dict's
``resolved_config`` JSONB — no live agent config needed.

Also provides lightweight disk-based config readers for decisions that
must be made at job *creation* time (before resolved_config exists),
such as scholar spawning.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo root for template resolution
# ---------------------------------------------------------------------------
def _find_repo_root() -> Path:
    """Walk up from this file to find the directory containing ``config/``."""
    anchor = Path(__file__).resolve().parent
    for _ in range(5):
        if (anchor / "config" / "defaults.yaml").is_file():
            return anchor
        anchor = anchor.parent
    # Last resort: assume working directory (WORKDIR /app in Docker)
    return Path.cwd()


_REPO_ROOT = _find_repo_root()


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


def _get_subjob_config(job: dict[str, Any], key: str) -> dict[str, Any]:
    """Extract a subjob config section (verification/curator/scholar) from resolved_config.

    Resolution order:
    1. ``resolved_config.agent.{key}`` — standard path after flatten
    2. ``resolved_config.agent.extra.{key}`` — fallback if extra wasn't flattened
    3. ``resolved_config.{key}`` — direct top-level path
    """
    rc = _parse_resolved_config(job)

    agent_block = rc.get("agent")
    if isinstance(agent_block, dict):
        # Primary: flattened into agent dict
        val = agent_block.get(key)
        if isinstance(val, dict):
            return val
        # Fallback: still nested in extra
        extra = agent_block.get("extra")
        if isinstance(extra, dict):
            val = extra.get(key)
            if isinstance(val, dict):
                return val

    # Top-level fallback
    val = rc.get(key)
    if isinstance(val, dict):
        return val

    return {}


def get_verification_config(job: dict[str, Any]) -> dict[str, Any]:
    """Extract verification config from resolved_config."""
    return _get_subjob_config(job, "verification")


def is_verification_enabled(job: dict[str, Any]) -> bool:
    """Check if verification is enabled for a job.

    Falls back to reading from disk if resolved_config is NULL.
    """
    cfg = get_verification_config(job)
    if cfg:
        return bool(cfg.get("enabled", False))
    # Disk fallback when resolved_config is missing
    config_name = job.get("config_name", "default")
    config_override = job.get("config_override")
    if isinstance(config_override, str):
        try:
            config_override = json.loads(config_override)
        except (json.JSONDecodeError, ValueError):
            config_override = None
    return bool(
        _resolve_config_section_from_disk(
            "verification", config_name, config_override
        ).get("enabled", False)
    )


def get_curation_config(job: dict[str, Any]) -> dict[str, Any]:
    """Extract curation config from resolved_config."""
    return _get_subjob_config(job, "curator")


def is_curation_enabled(job: dict[str, Any]) -> bool:
    """Check if curation is enabled for a job."""
    return bool(get_curation_config(job).get("enabled", False))


def get_scholar_config(job: dict[str, Any]) -> dict[str, Any]:
    """Extract scholar config from resolved_config."""
    return _get_subjob_config(job, "scholar")


def is_scholar_enabled(job: dict[str, Any]) -> bool:
    """Check if scholar is enabled for a job."""
    return bool(get_scholar_config(job).get("enabled", False))


# ---------------------------------------------------------------------------
# Disk-based config readers (for creation-time decisions)
# ---------------------------------------------------------------------------


def _resolve_config_section_from_disk(
    section: str,
    config_name: str,
    config_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lightweight YAML reader for a config section at creation/completion time.

    Reads just the ``section`` key from:

    1. ``config/defaults.yaml`` (base defaults)
    2. ``config/experts/{config_name}/config.yaml`` or
       ``config/{config_name}.yaml`` (expert override)
    3. ``config_override[section]`` (per-job override)

    This avoids importing the full config loader machinery.
    """
    result: dict[str, Any] = {}

    # 1. Read defaults
    defaults_path = _REPO_ROOT / "config" / "defaults.yaml"
    if defaults_path.exists():
        try:
            with open(defaults_path, encoding="utf-8") as f:
                defaults = yaml.safe_load(f) or {}
            if isinstance(defaults.get(section), dict):
                result.update(defaults[section])
        except Exception as e:
            logger.warning("Failed to read defaults.yaml for %s config: %s", section, e)

    # 2. Read expert config (overrides defaults)
    expert_paths = [
        _REPO_ROOT / "config" / "experts" / config_name / "config.yaml",
        _REPO_ROOT / "config" / f"{config_name}.yaml",
    ]
    for path in expert_paths:
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    expert = yaml.safe_load(f) or {}
                if isinstance(expert.get(section), dict):
                    result.update(expert[section])
            except Exception as e:
                logger.warning("Failed to read %s for %s config: %s", path, section, e)
            break

    # 3. Apply per-job config_override
    if config_override and isinstance(config_override.get(section), dict):
        result.update(config_override[section])

    return result


def resolve_scholar_config_from_disk(
    config_name: str,
    config_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Lightweight YAML reader for scholar config at job creation time."""
    return _resolve_config_section_from_disk("scholar", config_name, config_override)


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
    return freeze_type == "job_complete" or freeze_data.get("status") == "job_completed"


# ---------------------------------------------------------------------------
# Status determination
# ---------------------------------------------------------------------------


def determine_job_status(
    job: dict[str, Any],
    result: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Determine the new job status from the graph execution result.

    The orchestrator is the single authority for job status. Agents report
    facts (should_stop, goal_achieved, freeze_data) and this function
    determines the DB status.

    Returns:
        ``(new_status, error_message)`` — either or both may be ``None``
        to indicate no change is needed for that field.
    """
    error = result.get("error")
    should_stop = result.get("should_stop", False)
    goal_achieved = result.get("goal_achieved", False)

    if error:
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        return ("failed", error_msg)

    if not should_stop:
        return (None, None)  # Still running — leave as processing

    # Resolve freeze_data from DB or request body
    fd = _parse_freeze_data(job)
    if not fd:
        fd = result.get("freeze_data")
        if not isinstance(fd, dict):
            fd = {}

    # Critic jobs (have parent_job_id): read status from freeze_data.
    # Approved → "completed", returned → "waiting".
    if job.get("parent_job_id") is not None:
        fd_status = fd.get("status")
        if fd_status:
            # Normalize synonyms
            if fd_status == "job_completed":
                fd_status = "completed"
            logger.debug(
                "Job %s is a sub-job — setting status from freeze_data: %s",
                job.get("id"),
                fd_status,
            )
            return (fd_status, None)
        # No explicit status in freeze_data — infer from goal_achieved
        return ("completed" if goal_achieved else "pending_review", None)

    # Check if this is a job completion.
    # freeze_data may come from the DB (job dict) or from the request (result).
    freeze_type = fd.get("freeze_type")
    is_completion = (
        goal_achieved
        or freeze_type == "job_complete"
        or fd.get("status") == "job_completed"
    )

    # Job completion (any autonomy level)
    if is_completion:
        if is_verification_enabled(job):
            return ("reviewing", None)
        elif goal_achieved:
            return ("completed", None)
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


def format_scholar_instructions(
    parent_job_id: str,
    description: str,
    config_name: str,
    instructions: str | None = None,
    output_dir: str = "research",
) -> str | None:
    """Load and format the scholar subjob instructions template.

    Template is loaded from ``config/experts/scholar/scholar_subjob_instructions.md``
    with fallback to ``config/templates/scholar_subjob_instructions.md``.
    """
    search_paths = [
        _REPO_ROOT
        / "config"
        / "experts"
        / "scholar"
        / "scholar_subjob_instructions.md",
        _REPO_ROOT / "config" / "templates" / "scholar_subjob_instructions.md",
    ]

    template_path = None
    for path in search_paths:
        if path.exists():
            template_path = path
            break

    if not template_path:
        logger.error(
            "Scholar instructions template not found. Searched: %s",
            [str(p) for p in search_paths],
        )
        return None

    try:
        template = template_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("Failed to read scholar template %s: %s", template_path, e)
        return None

    if instructions:
        parent_instructions_section = f"## Additional Instructions\n\n{instructions}"
    else:
        parent_instructions_section = ""

    try:
        return template.format(
            parent_job_id=parent_job_id,
            parent_config=config_name,
            parent_description=description,
            parent_instructions_section=parent_instructions_section,
            output_dir=output_dir,
        )
    except KeyError as e:
        logger.error("Scholar template has unknown placeholder: %s", e)
        return None
