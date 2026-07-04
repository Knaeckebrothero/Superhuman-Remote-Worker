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
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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


# Bounded re-dispatch cap for memory/KB-unavailable pauses. After this many
# pause+retry cycles a memory-required job is failed instead of looping forever
# (docs/done/embedding_key_missing_silently_disables_memory_and_kb.md).
MEMORY_RETRY_CAP = 2


# Freeze types whose 'paused' means "park for the AUTO-dispatcher to re-dispatch"
# — as opposed to pauses awaiting an explicit human/sudo action
# (vm_upgrade_required) or user feedback. For these, /complete must clear the
# job row's freeze_data (stashing it in context.last_freeze_data): the
# dispatcher's get_dispatchable_jobs predicate requires ``freeze_data IS NULL``
# (partial-index contract, migration 0046), so a kept freeze blob makes the job
# permanently invisible to auto-resume — the drain-wedge that stalled run-8's
# iter-6 developer. A stale row-level freeze also poisons the NEXT completion
# (``_parse_freeze_data`` prefers the DB copy over the request body).
AUTO_REDISPATCH_FREEZE_TYPES: frozenset[str] = frozenset(
    {
        "version_upgrade",
        "memory_unavailable",
        "kb_unavailable",
        "workspace_upgrade_required",
    }
)


def auto_continue_drain_update(
    context: dict[str, Any], freeze_data: dict[str, Any], *, cap: int
) -> tuple[int, Any, bool]:
    """Progress-aware drain counter for auto-continue re-dispatches (backstop).

    Defense-in-depth for the version_upgrade drain livelock
    (docs/issues/version_upgrade_drain_livelock.md). The agent-side resume-clear
    (src/agent.py) guarantees a re-dispatched auto-continue job re-enters the
    graph and advances a phase each cycle, so a subsequent re-freeze carries a
    NEW ``phase_number``. If the freeze ``phase_number`` STOPS changing across
    re-dispatches, that guarantee has broken and the job is spinning with no
    progress — which this counter detects so the caller can alert loudly.

    Pure decision logic (the caller performs the context write + the alert I/O),
    mirroring the ``dispatch_guards`` extraction pattern.

    Args:
        context: the job's ``context`` dict (reads prior counter + last phase).
        freeze_data: the freeze blob being processed (reads ``phase_number``).
        cap: alert threshold — ``should_alert`` is True once drains reach it.

    Returns:
        ``(drains, last_phase, should_alert)`` — the new consecutive-no-progress
        count, the phase to remember, and whether to alert. A changed or absent
        ``phase_number`` resets the count to 0 (progress / no signal).
    """
    cur_phase = freeze_data.get("phase_number")
    last_phase = context.get("auto_continue_last_phase")
    drains = int(context.get("auto_continue_drains", 0) or 0)
    if cur_phase is not None and cur_phase == last_phase:
        drains += 1
    else:
        drains = 0
    return drains, cur_phase, drains >= cap


# ---------------------------------------------------------------------------
# LLM-outage pause + backoff re-dispatch
# (docs/features/llm_outage_pause_and_backoff_redispatch.md)
#
# A transient LLM outage exhausts the worker's Tier-1 in-process retries and
# freezes the job (freeze_type=llm_unavailable). Rather than fail it, the
# orchestrator pauses it and an outage sweeper re-dispatches it on an
# exponential, Full-Jittered backoff so an overnight loop survives a
# multi-minute/hour provider outage and resumes from its checkpoint. Two
# ceilings guarantee a broken config can't park an iteration forever.
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    """Read an int from env with a fallback (blank/garbage → default)."""
    try:
        return int((os.getenv(name) or "").strip() or default)
    except (ValueError, TypeError):
        return default


# 24h continuous-outage duration ceiling (primary; the Temporal
# scheduleToCloseTimeout model). After this the job fails loudly.
LLM_OUTAGE_CEILING_SECONDS = _env_int("LLM_OUTAGE_CEILING_SECONDS", 86_400)
# Attempts backstop — defends against pathological fast re-fail loops whose
# short Full-Jitter draws could rack up attempts without much wall-clock.
LLM_OUTAGE_MAX_ATTEMPTS = _env_int("LLM_OUTAGE_MAX_ATTEMPTS", 60)
# Gap since the last failure that resets the attempt counter + duration ceiling
# (the job ran fine in between → treat the next failure as a brand-new outage).
# MUST exceed LLM_OUTAGE_BACKOFF_CAP_SECONDS: during a single sustained outage,
# consecutive failures are ~one backoff apart (up to the 60-min cap), so a
# window <= the cap would misread that idle backoff wait as "ran fine" and
# spuriously reset first_failed_at every long cycle — letting a 24h outage park
# a loop forever. 2h > 1h cap leaves headroom while still resetting on a genuine
# multi-hour productive stretch between unrelated outages.
LLM_OUTAGE_RESET_WINDOW_SECONDS = _env_int("LLM_OUTAGE_RESET_WINDOW_SECONDS", 7_200)
# Backoff envelope: min(cap, base * 2**(attempt-1)), coefficient 2.
LLM_OUTAGE_BACKOFF_BASE_SECONDS = _env_int("LLM_OUTAGE_BACKOFF_BASE_SECONDS", 30)
LLM_OUTAGE_BACKOFF_CAP_SECONDS = _env_int("LLM_OUTAGE_BACKOFF_CAP_SECONDS", 3_600)
# Jitter strategy: "full" (uniform[0, envelope], the thundering-herd default) or
# "equal" (envelope/2 + uniform[0, envelope/2], a >=50% floor).
LLM_OUTAGE_JITTER = (os.getenv("LLM_OUTAGE_JITTER") or "full").strip().lower() or "full"


def _parse_context(job: dict[str, Any]) -> dict[str, Any]:
    """Parse the job's ``context`` JSONB (handles str or dict)."""
    ctx = job.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            return {}
    return ctx if isinstance(ctx, dict) else {}


def _parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp string into an aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def evaluate_llm_outage(ctx: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Decide an ``llm_unavailable`` freeze: apply the auto-reset + give-up ceiling.

    Pure function of the job's parsed ``context`` and an aware UTC ``now`` — no
    I/O — so the ``/complete`` handler and the sweeper backstop reach the same
    verdict. Returns the post-reset, PRE-increment outage state:

    * ``attempt``          — pauses so far for THIS outage (0 after a reset)
    * ``first_failed_at``  — outage start (``now`` on a fresh/reset outage)
    * ``elapsed_seconds``  — outage duration so far
    * ``reset``            — the 30-min gap reset fired
    * ``over_ceiling``     — the duration OR attempts ceiling has tripped
    * ``ceiling_reason``   — ``"duration"`` | ``"attempts"`` | ``None``
    """
    outage = ctx.get("llm_outage")
    outage = outage if isinstance(outage, dict) else {}
    attempt = int(outage.get("attempt", 0) or 0)
    first_failed_at = _parse_ts(outage.get("first_failed_at"))
    last_failed_at = _parse_ts(outage.get("last_failed_at"))

    reset = (
        last_failed_at is not None
        and (now - last_failed_at).total_seconds() > LLM_OUTAGE_RESET_WINDOW_SECONDS
    )
    if reset:
        attempt = 0
        first_failed_at = None
    if first_failed_at is None:
        first_failed_at = now

    elapsed = (now - first_failed_at).total_seconds()
    ceiling_reason: str | None = None
    if elapsed >= LLM_OUTAGE_CEILING_SECONDS:
        ceiling_reason = "duration"
    elif attempt >= LLM_OUTAGE_MAX_ATTEMPTS:
        ceiling_reason = "attempts"

    return {
        "attempt": attempt,
        "first_failed_at": first_failed_at,
        "elapsed_seconds": elapsed,
        "reset": reset,
        "over_ceiling": ceiling_reason is not None,
        "ceiling_reason": ceiling_reason,
    }


def llm_outage_backoff_seconds(
    attempt: int,
    *,
    base: int | None = None,
    cap: int | None = None,
    jitter: str | None = None,
    retry_after_seconds: float | None = None,
    rng: Callable[[float, float], float] | None = None,
) -> float:
    """Seconds to wait before the next re-dispatch for outage ``attempt`` (1-indexed).

    Deterministic envelope ``min(cap, base * 2**(attempt-1))`` (coefficient 2 —
    the universal default), then jitter:

    * ``full``  — ``uniform(0, envelope)`` (AWS Full Jitter; flattens a
      fleet-wide thundering herd across the whole window)
    * ``equal`` — ``envelope/2 + uniform(0, envelope/2)`` (>=50% floor)

    A server-directed ``retry_after_seconds`` (Retry-After /
    ``anthropic-ratelimit-*-reset``) floors the result. ``rng`` is injectable
    (signature ``uniform(a, b)``) for deterministic tests.
    """
    base = LLM_OUTAGE_BACKOFF_BASE_SECONDS if base is None else base
    cap = LLM_OUTAGE_BACKOFF_CAP_SECONDS if cap is None else cap
    jitter = (LLM_OUTAGE_JITTER if jitter is None else jitter).lower()
    _uniform = rng or random.uniform

    n = max(1, int(attempt))
    # Cap the exponent so an absurd attempt can't overflow the shift (the
    # envelope caps the value anyway).
    envelope = min(float(cap), float(base) * (2 ** min(n - 1, 30)))
    if jitter == "equal":
        delay = envelope / 2 + _uniform(0, envelope / 2)
    else:  # full jitter (default)
        delay = _uniform(0, envelope)
    if retry_after_seconds is not None:
        delay = max(delay, float(retry_after_seconds))
    return delay


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
    if freeze_type == "delegation":
        return ("waiting", None)
    if freeze_type == "vm_upgrade_required":
        return ("paused", None)
    if freeze_type == "workspace_upgrade_required":
        # A worker job requested an in-process sandbox upgrade, but the agent's
        # upgrade attempt failed (provision/seed/grant) and surfaced the freeze
        # instead of swapping in place (workspace_tier_upgrade.md §4.3 W1). Pause
        # so the dispatcher can re-attempt, mirroring the vm_upgrade fallback. On
        # the happy path this freeze never reaches here — the agent handles it
        # in-process and the job continues without ever reporting the freeze.
        return ("paused", None)
    if freeze_type == "version_upgrade":
        # Continue-as-New: agent observed orchestrator-set drain intent,
        # froze cleanly at a phase boundary, and the same job context
        # gets re-dispatched to a fresh-version agent by the auto-assign
        # dispatcher. State preserved through ``freeze_data`` + workspace.
        return ("paused", None)
    if freeze_type in ("memory_unavailable", "kb_unavailable"):
        # A memory-required job (e.g. the self-improvement loop) lost its
        # embedding-backed stores at startup. Pause for bounded re-dispatch so a
        # transient dispatch-time credential miss self-heals on the next pod;
        # after MEMORY_RETRY_CAP attempts fail loudly instead of looping. The
        # /complete handler bumps context.memory_retry_count on each pause.
        ctx = job.get("context") or {}
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except (json.JSONDecodeError, ValueError):
                ctx = {}
        retries = int((ctx or {}).get("memory_retry_count", 0) or 0)
        if retries < MEMORY_RETRY_CAP:
            return ("paused", None)
        reason = fd.get("reason") or "memory/KB unavailable at startup."
        return (
            "failed",
            f"{reason} Still unavailable after {retries} re-dispatch attempt(s) "
            f"— check the embedding model/endpoint (Admin → Models).",
        )
    if freeze_type == "llm_unavailable":
        # A transient LLM outage exhausted the worker's Tier-1 in-process
        # retries. Pause (non-terminal) so the outage sweeper re-dispatches the
        # SAME job on a backoff and it resumes from its checkpoint when the
        # endpoint recovers. After the 24h duration ceiling (or the attempts
        # backstop) fail loudly — a broken config must not park an overnight
        # loop iteration forever. The /complete handler owns the increment +
        # next_retry_at; here we only make the pause-vs-fail call.
        # docs/features/llm_outage_pause_and_backoff_redispatch.md
        ev = evaluate_llm_outage(_parse_context(job), datetime.now(timezone.utc))
        if ev["over_ceiling"]:
            summary = (
                fd.get("error_summary")
                or fd.get("reason")
                or "LLM endpoint unavailable"
            )
            model = fd.get("model")
            if ev["ceiling_reason"] == "duration":
                detail = (
                    f"still unavailable after ~{LLM_OUTAGE_CEILING_SECONDS / 3600:.0f}h "
                    f"of continuous outage"
                )
            else:
                detail = f"still unavailable after {ev['attempt']} re-dispatch attempts"
            return (
                "failed",
                f"LLM endpoint unavailable — {detail}. Giving up. Last error: "
                f"{str(summary)[:200]}"
                + (f" (model '{model}')" if model else "")
                + ". Check the model endpoint/provider (Admin → Models).",
            )
        return ("paused", None)
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
