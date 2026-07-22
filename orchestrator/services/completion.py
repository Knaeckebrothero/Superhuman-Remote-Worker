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

import hashlib
import json
import logging
import os
import random
import re
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
        if (anchor / "config" / "worker_base.yaml").is_file():
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
    config_name = job.get("config_name") or "worker_base"
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

    1. ``config/worker_base.yaml`` (base defaults)
    2. ``config/experts/{config_name}/config.yaml`` or
       ``config/{config_name}.yaml`` (expert override)
    3. ``config_override[section]`` (per-job override)

    This avoids importing the full config loader machinery.
    """
    result: dict[str, Any] = {}

    # 1. Read defaults
    defaults_path = _REPO_ROOT / "config" / "worker_base.yaml"
    if defaults_path.exists():
        try:
            with open(defaults_path, encoding="utf-8") as f:
                defaults = yaml.safe_load(f) or {}
            if isinstance(defaults.get(section), dict):
                result.update(defaults[section])
        except Exception as e:
            logger.warning(
                "Failed to read worker_base.yaml for %s config: %s", section, e
            )

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

# Freeze types whose dedicated branch in ``determine_job_status`` must own the
# pause-vs-fail decision even when a coincident ``error`` rides the same
# completion report. A drain/outage freeze taken at a clean phase boundary beats
# a transient interrupt error — the work is checkpointed and re-dispatch resumes
# it — whereas the bare ``if error`` short-circuit would hard-fail an otherwise
# re-dispatchable job (observed: a deploy drain racing a SIGTERM-interrupt
# failing loop iterations instead of pausing them). These freezes' own branches
# still enforce their fail conditions (memory/KB retry caps, the LLM-outage
# duration ceiling), so guarding the short-circuit does not turn a genuine give-up into a
# pause. ``llm_unavailable`` is included alongside the auto-redispatch set (same
# failure class — the outage sweeper re-dispatches it).
# docs/done/version_upgrade_drain_masked_by_coincident_error.md
ERROR_IMMUNE_FREEZE_TYPES: frozenset[str] = AUTO_REDISPATCH_FREEZE_TYPES | frozenset(
    {"llm_unavailable"}
)

# Parent statuses that PERMANENTLY block a subjob's re-dispatch. The dispatcher's
# cascade guard (get_dispatchable_jobs, postgres.py) also blocks on a 'paused'
# ancestor, but that is temporary — the subjob re-dispatches once the parent
# resumes — so only the permanent terminals wedge a paused subjob forever. A
# drain-frozen subjob under such a parent must resolve terminally, not pause.
# docs/done/coincident_infra_error_overrides_reported_job_outcome.md
_PARENT_TERMINAL_BLOCKING: frozenset[str] = frozenset({"failed", "cancelled"})

# Freeze types whose subjob short-circuit routes to the shared re-dispatch
# handling instead of the pending_review fallback: the drain freeze pauses
# directly, and the outage freezes (memory/kb/llm) fall through to their
# type-specific branches — whose retry caps and duration ceilings are
# row-scoped (context.memory_retry_count / context.llm_outage on the subjob's
# own row), so they apply per-subjob unchanged. All are guarded by
# _PARENT_TERMINAL_BLOCKING first: a paused subjob under a permanently-dead
# parent is a silent cascade-guard wedge.
# docs/features/llm_outage_subjob_resilience.md
_SUBJOB_REDISPATCH_FREEZE_TYPES: frozenset[str] = frozenset(
    {
        "version_upgrade",
        "memory_unavailable",
        "kb_unavailable",
        "llm_unavailable",
    }
)

# Substrings that mark an error as a workspace/VM *teardown* (connectivity) blip
# rather than a genuine mid-run failure. On completion the VM is reaped, and a
# trailing SSH/SFTP/stat op can time out against the gone workspace; that trailing
# error must not override an outcome the agent already reported as complete.
# Kept deliberately narrow (connectivity/teardown only) — widen on evidence, not
# on suspicion (the "cleanup hiccup vs real failure" line, signed off 2026-07-12).
# docs/done/coincident_infra_error_overrides_reported_job_outcome.md
_TEARDOWN_ERROR_PATTERNS: tuple[str, ...] = (
    "failed to connect to workspace",
    "workspace i/o timed out",
    "workspace is unavailable",
    "key-exchange timed out",
    "waiting for key negotiation",
    "[gone]",
)


def is_teardown_infra_error(message: str | None) -> bool:
    """True if ``message`` looks like a workspace/VM teardown-connectivity blip.

    Used to tell a "finished the work, then a teardown hiccup" completion report
    apart from a genuine mid-run crash: only the former may keep its reported
    (successful) outcome when a coincident error rides the same report.
    """
    if not message:
        return False
    low = message.lower()
    return any(pattern in low for pattern in _TEARDOWN_ERROR_PATTERNS)


def classify_workspace_death(
    termination: dict[str, Any] | None,
) -> tuple[str, bool]:
    """Human cause + is-resource-kill flag from a workspace pod's terminated
    state (the dict from ``ContainerProvisioner.get_last_termination``).

    The bool is the escalation signal: a resource kill (OOM / node-pressure
    eviction) means "retry bigger", not "re-dispatch into the same grave". We
    only assert a resource kill on positive evidence — a pod already gone before
    we could read it stays ``False`` (no evidence), so we never fabricate an OOM.
    See docs/features/workspace_resource_pressure_resilience.md.
    """
    if not termination:
        return (
            "workspace pod gone before its termination cause could be read",
            False,
        )
    container_reason = termination.get("container_reason") or ""
    pod_reason = termination.get("pod_reason") or ""
    exit_code = termination.get("exit_code")
    if container_reason == "OOMKilled":
        return ("workspace ran out of memory (OOMKilled)", True)
    if pod_reason == "Evicted":
        return ("workspace evicted under node resource pressure", True)
    if exit_code == 137:
        # 137 = 128 + SIGKILL. A workspace pod has no liveness probe to SIGKILL
        # it, so a bare 137 without an OOMKilled reason is still almost always
        # memory pressure (the kernel can drop the reason when the node is under
        # duress). Flag as a likely resource kill — hedged wording, not a claim.
        return (
            "workspace process killed (SIGKILL/137) — likely memory pressure",
            True,
        )
    if container_reason:
        detail = f"workspace container terminated: {container_reason}"
        if exit_code is not None:
            detail += f" (exit {exit_code})"
        return (detail, False)
    return ("workspace became unavailable (no terminated-container record)", False)


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


# 12h continuous-outage duration ceiling (primary; the Temporal
# scheduleToCloseTimeout model). After this the job fails loudly. Doubles as the
# cooldown pause-vs-fail-fast cutoff (docs/features/llm_cooldown_pause_and_resume.md):
# a quota cooldown whose provider-stated reset exceeds this fails fast instead of
# pausing. Keep the 43_200 default in sync with src/graph.py's
# _COOLDOWN_MAX_PAUSE_SECONDS (a different pod reads the same env var).
LLM_OUTAGE_CEILING_SECONDS = _env_int("LLM_OUTAGE_CEILING_SECONDS", 43_200)
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


def llm_outage_fingerprint(fd: dict[str, Any]) -> str | None:
    """Fingerprint a deterministic request-rejection error, or None.

    Feeds the fail-fast in ``determine_job_status``: an ``llm_unavailable``
    pause whose normalized error text is IDENTICAL to the previous pause's is
    deterministic — retrying cannot fix the input (the 2026-07-11 `6a186c76`
    poisoned-tool-call incident burned 12 requests + would have burned the
    whole give-up ceiling; see docs/features/outbound_message_hygiene.md).

    Deliberately narrow: only request-shaped 4xx rejections qualify. A genuine
    endpoint outage (timeouts, connection errors, 5xx) repeats an identical
    generic message the whole time the provider is down and MUST keep
    pausing/retrying — that is the outage feature's entire purpose. 429/rate
    texts are excluded too (they have their own handling upstream).

    Normalization strips long ids (request ids, tool_call ids, uuids) and all
    digits so two cycles of the same rejection hash identically.

    ``deterministic_exempt`` (set by the agent when the failing response was
    edge-shaped — a non-API body such as an nginx 404 page) opts the freeze
    out entirely: an identical edge page across pause cycles means the
    provider's gateway is still down, not that the request is deterministic,
    so the job must keep riding the outage ceilings like a 5xx outage. See
    docs/issues/llm_infra_404_misclassified_permanent_kills_jobs.md.
    """
    if fd.get("deterministic_exempt"):
        return None
    src = str(fd.get("error_summary") or fd.get("reason") or "")
    if not src:
        return None
    low = src.lower()
    if "429" in low or "rate limit" in low or "too many requests" in low:
        return None
    if not re.search(r"\b4\d\d\b", low):
        return None
    # {20,} not shorter: ids (uuids, request ids, tool_call ids) are 20+ chars
    # while semantic tokens like 'bad_request_error' (17) must survive so
    # different error TYPES keep distinct fingerprints.
    norm = re.sub(r"[a-z0-9_-]{20,}", "<tok>", low)
    norm = re.sub(r"\d+", "<n>", norm)
    return hashlib.sha256(norm[:500].encode("utf-8")).hexdigest()[:16]


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
    next_retry_at = _parse_ts(outage.get("next_retry_at"))

    # Measure idle time from when the job was free to run again — the END of any
    # scheduled wait we imposed (next_retry_at) — not from the last failure. A
    # long cooldown pause (retry_after floored to hours) is us deliberately
    # sleeping, not the job running fine; anchoring on last_failed_at would
    # misread it as a productive gap, spuriously reset the ceiling, and let a
    # never-clearing cooldown park the loop forever. next_retry_at is absent on
    # legacy state → fall back to last_failed_at (byte-for-byte today's behavior).
    # docs/features/llm_cooldown_pause_and_resume.md §Design decision
    anchor = last_failed_at
    if next_retry_at is not None and (anchor is None or next_retry_at > anchor):
        anchor = next_retry_at
    reset = (
        anchor is not None
        and (now - anchor).total_seconds() > LLM_OUTAGE_RESET_WINDOW_SECONDS
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
    *,
    parent_status: str | None = None,
) -> tuple[str | None, str | None]:
    """Determine the new job status from the graph execution result.

    The orchestrator is the single authority for job status. Agents report
    facts (should_stop, goal_achieved, freeze_data) and this function
    determines the DB status.

    Args:
        parent_status: For a subjob (``parent_job_id`` set), the parent's current
            status. Lets a drain-frozen subjob resolve terminally instead of
            pausing into a cascade-guard wedge when the parent has permanently
            failed. ``None`` for top-level jobs (or when unknown).

    Returns:
        ``(new_status, error_message)`` — either or both may be ``None``
        to indicate no change is needed for that field.
    """
    error = result.get("error")
    should_stop = result.get("should_stop", False)
    goal_achieved = result.get("goal_achieved", False)

    # Resolve freeze_data (DB row preferred, request-body fallback) BEFORE the
    # error short-circuit so it can consult an auto-redispatch / outage freeze.
    fd = _parse_freeze_data(job)
    if not fd:
        fd = result.get("freeze_data")
        if not isinstance(fd, dict):
            fd = {}
    freeze_type = fd.get("freeze_type")

    # Did the agent's report DECLARE a genuine completion? Resolved up-front so the
    # error short-circuit can tell a "finished the work, then a teardown blip"
    # report apart from a mid-run crash — the former keeps its completed outcome.
    is_completion = (
        goal_achieved
        or freeze_type == "job_complete"
        or fd.get("status") == "job_completed"
    )

    if error:
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        # Terminal-status idempotency backstop: a job that ALREADY reached success
        # must never be downgraded to 'failed' by a late/duplicate report or a
        # post-completion teardown error — a second /complete, or a trailing
        # SSH/IO timeout after the completion side-effects already merged the work.
        # (Carve-out (2) below handles the single-report dual-payload case; this
        # covers the already-persisted row regardless of which trailing op raised
        # the error.) Return "no change" so the successful status stands.
        # docs/done/coincident_infra_error_overrides_reported_job_outcome.md
        if job.get("status") == "completed" or job.get("merge_status") == "merged":
            logger.warning(
                "Job %s: ignoring a coincident error on an already-successful job "
                "(status=%s, merge_status=%s): %s",
                job.get("id"),
                job.get("status"),
                job.get("merge_status"),
                error_msg[:200],
            )
            return (None, None)
        # A coincident error must not mask an outcome the agent already reported.
        # Two carve-outs let the report's own branch below own the decision:
        #  (1) a clean-boundary auto-redispatch / outage freeze (drain
        #      Continue-as-New, memory/KB retry caps, LLM-outage ceiling) — the
        #      work is checkpointed and re-dispatch resumes it. For a SUBJOB the
        #      carve-out additionally requires a live parent and one of the
        #      subjob-redispatch freeze types (a dead parent means the pause
        #      could never resume — fail on the error as before). A stale row
        #      freeze can't shield a crashed run (should_stop is False there →
        #      falls through to failed).
        #      docs/done/version_upgrade_drain_masked_by_coincident_error.md
        #      docs/features/llm_outage_subjob_resilience.md
        #  (2) a genuine completion whose only error is a post-completion
        #      workspace/VM *teardown* blip: the VM is reaped on completion and a
        #      trailing SSH/IO op then times out against the gone workspace. The
        #      deliverables already landed (merge/graft ran); the completion branch
        #      below resolves it exactly as it would with no error. A NON-teardown
        #      error (real mid-run crash) still fails, even on a completion report.
        #      docs/done/coincident_infra_error_overrides_reported_job_outcome.md
        redispatchable = should_stop and (
            (
                job.get("parent_job_id") is None
                and freeze_type in ERROR_IMMUNE_FREEZE_TYPES
            )
            or (
                job.get("parent_job_id") is not None
                and freeze_type in _SUBJOB_REDISPATCH_FREEZE_TYPES
                and parent_status not in _PARENT_TERMINAL_BLOCKING
            )
        )
        completed_despite_teardown = (
            should_stop and is_completion and is_teardown_infra_error(error_msg)
        )
        if not (redispatchable or completed_despite_teardown):
            return ("failed", error_msg)
        logger.warning(
            "Job %s: routing the reported outcome over a coincident error "
            "(freeze_type=%r, is_completion=%s, teardown=%s): %s",
            job.get("id"),
            freeze_type,
            is_completion,
            completed_despite_teardown,
            error_msg[:200],
        )

    if not should_stop:
        return (None, None)  # Still running — leave as processing

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
        # A drain (version_upgrade) or outage (memory/kb/llm) freeze on a subjob
        # is re-dispatchable exactly like a top-level job — this short-circuit
        # historically routed them to pending_review because the freeze table
        # below was unreachable for subjobs. Guard the parent-terminal case
        # first: if the parent has permanently failed, the dispatcher's cascade
        # guard will never re-dispatch the subjob (a silent paused wedge,
        # strictly worse than the visible pending_review), so resolve terminally
        # instead. The drain freeze pauses here; the outage freezes fall THROUGH
        # to the shared type-specific branches below — their retry caps and
        # duration ceilings live on the subjob's own row
        # (context.memory_retry_count / context.llm_outage), so they apply
        # per-subjob unchanged.
        # docs/done/coincident_infra_error_overrides_reported_job_outcome.md
        # docs/features/llm_outage_subjob_resilience.md
        if freeze_type in _SUBJOB_REDISPATCH_FREEZE_TYPES:
            if parent_status in _PARENT_TERMINAL_BLOCKING:
                return ("completed" if goal_achieved else "cancelled", None)
            if freeze_type == "version_upgrade":
                return ("paused", None)
            # memory/kb/llm: shared branches below own pause-vs-fail.
        else:
            # No explicit status in freeze_data — infer from goal_achieved
            return ("completed" if goal_achieved else "pending_review", None)

    # Unattended project-loop jobs must never land on the human-review gate:
    # the loop's advance hook fires only on TERMINAL statuses, so a
    # pending_review loop job wedges the whole loop forever (the same
    # invisible-wedge class as the Mode-A diff gate, which is exempted
    # narrowly in the /complete handler). Quality judgment for loop work
    # belongs to the retro + the next critic, and lost work is already
    # flagged by the empty-merge (F29) check — so a loop job that stops
    # without declaring completion maps to `completed` (weak, but honest in
    # its retro), never `pending_review`. Found live: a campaign member that
    # finished without freeze_data wedged the P1 smoke loop.
    # docs/features/loop_campaign_scheduling.md.
    from services.project_loops import job_loop_id

    is_loop_job = bool(job_loop_id(job))

    # Job completion (any autonomy level)
    if is_completion:
        if is_verification_enabled(job):
            return ("reviewing", None)
        elif goal_achieved or is_loop_job:
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
        # endpoint recovers. After the duration ceiling (12h default,
        # LLM_OUTAGE_CEILING_SECONDS) or the attempts
        # backstop, fail loudly — a broken config must not park an overnight
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
        # Determinism fail-fast: a request-shaped 4xx whose normalized text is
        # identical to the PREVIOUS pause's is deterministic — a third, fourth,
        # ... cycle can only burn the give-up ceiling. Genuine outages never
        # enter here (llm_outage_fingerprint returns None for connection/5xx/
        # rate errors). docs/features/outbound_message_hygiene.md (layer 3).
        fp = llm_outage_fingerprint(fd)
        if fp is not None:
            prior = _parse_context(job).get("llm_outage")
            if isinstance(prior, dict) and prior.get("fingerprint") == fp:
                summary = fd.get("error_summary") or fd.get("reason") or "LLM error"
                return (
                    "failed",
                    "Deterministic LLM request rejection — the identical "
                    "(normalized) error failed two consecutive backoff cycles; "
                    f"retrying cannot fix this input. Last error: "
                    f"{str(summary)[:200]}. The request/history is likely "
                    "poisoned or the model config is wrong — resume with "
                    "feedback (compacts context) or fix the model "
                    "(Admin → Models).",
                )
        return ("paused", None)
    if is_loop_job:
        # Non-completion stop with no recognized freeze on an unattended loop
        # job (agent stopped without declaring job_complete — weak-model
        # finishes do this). `completed` keeps the loop advancing; the retro
        # records the actual (possibly poor) state and the empty-merge check
        # flags lost work. See the loop-wedge rationale above.
        logger.warning(
            "Loop job %s stopped without a completion declaration "
            "(freeze_type=%r) — mapping to 'completed' so the loop advances",
            job.get("id"),
            freeze_type,
        )
        return ("completed", None)
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
