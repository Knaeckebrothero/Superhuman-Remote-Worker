"""One job-liveness contract (officer_supervision_surface §5, slice E3).

``compute_job_liveness`` is the single server-side answer to "is this job
alive?", consumed by:

- ``GET /api/jobs/{id}/progress`` (the corrected get_job_progress);
- ``GET /api/stats/stuck`` (get_stuck_jobs);
- the SITREP active-job lines (``services/sitrep.py``);
- (later) officer_backlog_pools' stale-claim age — the batch entry point is
  deliberately project-scoped-batch-friendly for it.

Inputs in descending authority:

1. terminal/control-plane status and the explicit pause/wait/freeze reason;
2. audit last-write (the audit store's time range for the job);
3. agent binding and heartbeat freshness;
4. transport-specific evidence freshness (not consulted in v1).

``jobs.updated_at`` is display metadata, NEVER liveness — it is poisoned by
trigger cascades and wake-state bookkeeping (the same reason SITREP rejected
it). No branch here converts missing telemetry into a fabricated fact: when
the audit store is down and no heartbeat evidence exists, the state is
``unavailable`` — not 0%, not stuck.

Threshold policy lives HERE, once (``JOB_LIVENESS_STALL_MINUTES``), so the
SITREP cannot call a job healthy while get_stuck_jobs calls it stuck.

States: ``active | waiting | paused | suspected_stuck | unavailable``
plus ``terminal`` for completed/failed/cancelled jobs — a deliberate
extension of the §5 set, because reporting a completed job as "waiting"
would itself be a manufactured fact.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The one stall threshold (minutes without observed activity before a
#: processing job is *suspected* stuck). Env-tunable, consumed by every
#: surface; per-call overrides (the get_stuck_jobs tool argument) narrow or
#: widen the same computation rather than forking it.
STALL_THRESHOLD_MINUTES = int(os.environ.get("JOB_LIVENESS_STALL_MINUTES", "30"))

#: Agent heartbeats arrive every ~60s and agents are marked offline after
#: 3 minutes — a heartbeat older than that is stale binding evidence.
HEARTBEAT_FRESH_SECONDS = int(os.environ.get("JOB_LIVENESS_HEARTBEAT_FRESH", "180"))

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _iso(ts: Optional[datetime]) -> Optional[str]:
    return ts.isoformat() if ts else None


def _age_minutes(ts: datetime, now: datetime) -> float:
    return max(0.0, (now - ts).total_seconds() / 60.0)


def _parse_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _freeze_reason(job: dict[str, Any]) -> Optional[str]:
    freeze = _parse_json_field(job.get("freeze_data"))
    if not freeze:
        freeze = _parse_json_field(
            _parse_json_field(job.get("context")).get("freeze_data")
        )
    if not freeze:
        return None
    for key in ("reason", "message", "status_message", "review_reason", "pause_reason"):
        value = freeze.get(key)
        if value:
            return str(value)
    freeze_type = freeze.get("freeze_type") or freeze.get("type")
    return f"freeze type '{freeze_type}'" if freeze_type else None


async def _audit_last_write(
    audit_reader: Any, job_id: str
) -> tuple[Optional[datetime], bool]:
    """(last audit write, audit_reachable). Unreachable ≠ empty."""
    if audit_reader is None or not getattr(audit_reader, "is_available", False):
        return None, False
    try:
        time_range = await audit_reader.get_audit_time_range(job_id)
    except Exception:  # noqa: BLE001 — degraded store is a classified outcome
        logger.warning(
            "liveness: audit time range failed for %s", job_id, exc_info=True
        )
        return None, False
    if not time_range:
        return None, True
    return _aware(time_range.get("end")), True


async def _agent_heartbeat(
    db: Any, agent_id: Any, cache: dict[str, Any] | None
) -> tuple[Optional[datetime], bool]:
    """(last heartbeat, lookup_succeeded) for the bound agent."""
    if db is None or not agent_id:
        return None, False
    key = str(agent_id)
    if cache is not None and key in cache:
        agent = cache[key]
    else:
        try:
            agent = await db.get_agent(key)
        except Exception:  # noqa: BLE001
            logger.warning("liveness: agent lookup failed for %s", key, exc_info=True)
            agent = None
        if cache is not None:
            cache[key] = agent
    if not agent:
        return None, False
    return _aware(agent.get("last_heartbeat")), True


async def compute_job_liveness(
    job: dict[str, Any],
    *,
    audit_reader: Any = None,
    db: Any = None,
    threshold_minutes: int | None = None,
    now: datetime | None = None,
    _agent_cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute the liveness verdict for one job row.

    Returns ``{state, observed_at, reasons, last_activity_at, threshold_minutes}``.
    """
    now = now or _now()
    threshold = (
        threshold_minutes if threshold_minutes is not None else STALL_THRESHOLD_MINUTES
    )
    status = str(job.get("status") or "unknown")
    job_id = str(job.get("id") or "")
    reasons: list[str] = []
    last_activity: Optional[datetime] = None
    # E1 sources: the control row is what we were handed; audit/heartbeat are
    # appended when (and only when) they are actually consulted.
    sources: list[dict[str, Any]] = [
        {"name": "control_db", "status": "fresh", "as_of": now.isoformat()}
    ]

    def verdict(state: str) -> dict[str, Any]:
        return {
            "state": state,
            "observed_at": now.isoformat(),
            "reasons": reasons,
            "last_activity_at": _iso(last_activity),
            "threshold_minutes": threshold,
            "sources": sources,
        }

    # Authority 1: terminal / explicit control-plane state.
    if status in TERMINAL_STATUSES:
        reasons.append(f"terminal status '{status}'")
        last_activity = _aware(job.get("completed_at")) or _aware(job.get("updated_at"))
        return verdict("terminal")
    if status == "paused":
        reasons.append("paused by control plane")
        freeze_reason = _freeze_reason(job)
        if freeze_reason:
            reasons.append(freeze_reason)
        return verdict("paused")
    if status != "processing":
        waiting_reason = {
            "created": "awaiting workspace provisioning and dispatch",
            "pending_review": "pending human review",
            "waiting_for_reply": "waiting for a human reply",
            "reviewing": "under critic review",
        }.get(status, f"control-plane status '{status}'")
        reasons.append(waiting_reason)
        freeze_reason = _freeze_reason(job)
        if freeze_reason:
            reasons.append(freeze_reason)
        return verdict("waiting")

    # status == processing — authority 2: audit movement.
    audit_end, audit_reachable = await _audit_last_write(audit_reader, job_id)
    heartbeat, agent_known = await _agent_heartbeat(
        db, job.get("assigned_agent_id"), _agent_cache
    )
    heartbeat_fresh = bool(
        heartbeat and (now - heartbeat).total_seconds() <= HEARTBEAT_FRESH_SECONDS
    )
    if not audit_reachable:
        sources.append(
            {
                "name": "audit_db",
                "status": "unavailable",
                "reason": "audit store unreachable or not configured",
            }
        )
    else:
        sources.append(
            {
                "name": "audit_db",
                "status": "fresh" if audit_end is not None else "empty",
                "as_of": _iso(audit_end) or now.isoformat(),
            }
        )
    if job.get("assigned_agent_id"):
        sources.append(
            {
                "name": "agent_heartbeat",
                "status": "fresh" if agent_known and heartbeat else "unavailable",
                **({"as_of": _iso(heartbeat)} if heartbeat else {}),
                **(
                    {}
                    if agent_known and heartbeat
                    else {"reason": "no heartbeat recorded for the assigned agent"}
                ),
            }
        )

    if audit_reachable and audit_end is not None:
        last_activity = audit_end
        age = _age_minutes(audit_end, now)
        if age <= threshold:
            reasons.append(f"audit activity {age:.0f}m ago")
            return verdict("active")
        reasons.append(f"no audit activity for {age:.0f}m (threshold {threshold}m)")
        if heartbeat_fresh:
            reasons.append("agent heartbeat fresh — pod alive but not progressing")
        elif agent_known and heartbeat is not None:
            reasons.append(
                f"agent heartbeat stale ({_age_minutes(heartbeat, now):.0f}m ago)"
            )
        elif job.get("assigned_agent_id"):
            reasons.append("no heartbeat recorded for the assigned agent")
        else:
            reasons.append("processing with no assigned agent")
        return verdict("suspected_stuck")

    if audit_reachable:
        # Audit store answered: this job simply has no rows yet.
        if heartbeat_fresh:
            last_activity = heartbeat
            reasons.append("no audit rows yet; agent heartbeat fresh")
            return verdict("active")
        if not job.get("assigned_agent_id"):
            reasons.append("processing with no assigned agent and no audit activity")
            return verdict("suspected_stuck")
        last_activity = heartbeat
        if heartbeat is not None:
            reasons.append(
                "no audit activity recorded; agent heartbeat stale "
                f"({_age_minutes(heartbeat, now):.0f}m ago)"
            )
        else:
            reasons.append("no audit activity recorded and no agent heartbeat")
        return verdict("suspected_stuck")

    # Audit store unreachable — authority 3 only.
    reasons.append("audit store unavailable")
    if heartbeat_fresh:
        last_activity = heartbeat
        reasons.append("agent heartbeat fresh")
        return verdict("active")
    if heartbeat is not None:
        last_activity = heartbeat
        reasons.append(
            f"agent heartbeat stale ({_age_minutes(heartbeat, now):.0f}m ago)"
        )
        return verdict("suspected_stuck")
    # No evidence in either direction: say so instead of inventing a verdict.
    reasons.append("no agent heartbeat evidence")
    return verdict("unavailable")


async def compute_jobs_liveness(
    jobs: list[dict[str, Any]],
    *,
    audit_reader: Any = None,
    db: Any = None,
    threshold_minutes: int | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Batch variant keyed by job id — one agent lookup per distinct agent.

    Project-scoped batches (SITREP, backlog stale-claim sweeps) call this so
    every surface shares one computation and one threshold.
    """
    now = now or _now()
    agent_cache: dict[str, Any] = {}
    results: dict[str, dict[str, Any]] = {}
    for job in jobs:
        job_id = str(job.get("id") or "")
        if not job_id:
            continue
        results[job_id] = await compute_job_liveness(
            job,
            audit_reader=audit_reader,
            db=db,
            threshold_minutes=threshold_minutes,
            now=now,
            _agent_cache=agent_cache,
        )
    return results


__all__ = [
    "HEARTBEAT_FRESH_SECONDS",
    "STALL_THRESHOLD_MINUTES",
    "TERMINAL_STATUSES",
    "compute_job_liveness",
    "compute_jobs_liveness",
]
