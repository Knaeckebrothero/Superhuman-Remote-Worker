"""Officer sitrep — the computed state delta injected at every wake.

The minimal ``[SITREP]`` renderer in ``session_wake._format_officer_wake``
listed the wake *reasons*; this module answers the officer's standing question
— "what changed since I last looked?" — so a wake starts from facts instead of
a fan-out of list/get tool calls (docs/features/centurion.md §4, S3).

Delta, not dashboard
--------------------
The sitrep diffs the project's job set against fingerprints stored on the
officer thread (``metadata.officer_state.sitrep``). The watermark advances
ONLY when the message was actually delivered to a live pod — a durable
transcript write is not a wake, and advancing on it would silently swallow
the delta the officer never read (implementation notes, risk 5). The caller
(``session_wake.drain_pending_event_wakes``) owns that rule: it merges the
returned state patch only on live-inject success.

Fingerprints are snapshot-diffs on status + audit step counts, deliberately
NOT ``jobs.updated_at`` — that column is poisoned by the gc trigger and
wake-state bookkeeping writes (schema comment on ``update_jobs_updated_at``).

Failure containment
-------------------
Every section renders inside its own try/except and degrades to a one-line
note (the ``stale_agent_detector._step`` isolation lesson: one poisoned
query must cost one section, not the wake). The audit DB and the usage
ledger are optional at startup, so their sections degrade cleanly to
"unavailable". A total failure returns ``(None, None)`` and the caller falls
back to the minimal renderer — a wake must never be lost to its formatter.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

# Line caps per section. The sitrep is a briefing, not a dump: full detail
# for what changed, one-liners for what didn't, tools for everything else.
_MAX_TRANSITIONS = 12
_MAX_NEW = 8
_MAX_ACTIVE = 8
_MAX_PENDING = 5
_DESC_CHARS = 120

# Statuses that count as "in flight" for the active-jobs detail and the
# capacity line. 'paused' is shown in transitions but does not hold a slot.
_ACTIVE_STATUSES = ("created", "processing")

_UNSET = object()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ago(ts: Optional[datetime], now: datetime) -> str:
    if ts is None:
        return "unknown"
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    seconds = max(0, int((now - ts).total_seconds()))
    if seconds < 90:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _resolve_handles() -> tuple[Any, Any]:
    """Late-bind the audit reader and usage ledger off the main module.

    Both are module globals constructed during orchestrator startup; importing
    ``main`` at call time (never at import time) avoids the circular import
    and simply yields ``(None, None)`` in test processes that never built
    them — which is exactly the degraded path the sections already handle.
    """
    try:
        import main as orchestrator_main

        return (
            getattr(orchestrator_main, "audit_reader", None),
            getattr(orchestrator_main, "usage_ledger", None),
        )
    except Exception:
        return None, None


def prior_state(thread: dict[str, Any]) -> dict[str, Any]:
    """The stored sitrep state on an officer thread ({} when none)."""
    metadata = _as_dict(thread.get("metadata"))
    officer_state = _as_dict(metadata.get("officer_state"))
    return _as_dict(officer_state.get("sitrep"))


async def build_wake_message(
    db: Any,
    thread: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    audit_reader: Any = _UNSET,
    usage_ledger: Any = _UNSET,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """Render the full sitrep for one coalesced officer wake.

    Returns ``(text, state_patch)``. The patch is the new fingerprint state
    for ``merge_thread_officer_state(thread_id, patch)`` and MUST only be
    written after a successful live delivery. ``(None, None)`` means total
    failure — fall back to the minimal renderer.
    """
    try:
        if audit_reader is _UNSET or usage_ledger is _UNSET:
            resolved_audit, resolved_ledger = _resolve_handles()
            if audit_reader is _UNSET:
                audit_reader = resolved_audit
            if usage_ledger is _UNSET:
                usage_ledger = resolved_ledger

        now = datetime.now(timezone.utc)
        thread_id = str(thread["id"])
        project_id = thread.get("project_id")
        project_id = str(project_id) if project_id else None
        prev = prior_state(thread)
        prev_prints = _as_dict(prev.get("fingerprints"))
        prev_watermark = _parse_iso(prev.get("watermark"))

        lines: list[str] = [_header(now, prev_watermark)]
        lines += _reason_lines(rows)

        jobs_lines, fingerprints = await _jobs_section(
            db, audit_reader, project_id, prev_prints, prev_watermark, now
        )
        lines += jobs_lines
        lines += await _pending_section(db, thread_id, project_id, now)
        lines += await _capacity_section(db, thread, thread_id)
        lines += await _fleet_section(db)
        lines += await _budget_section(usage_ledger, project_id, now)

        lines.append(
            "Assess, act within your authority, then file a sleep. Use "
            "notify_user if something needs the Legate."
        )
        patch = {
            "sitrep": {
                "watermark": now.isoformat(),
                "fingerprints": fingerprints,
            }
        }
        return "\n".join(lines), patch
    except Exception:
        logger.exception("sitrep: build failed — falling back to minimal renderer")
        return None, None


def _header(now: datetime, prev_watermark: Optional[datetime]) -> str:
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    if prev_watermark is None:
        return f"[SITREP] {stamp} — first sitrep (no prior watermark)"
    return f"[SITREP] {stamp} — delta since {_ago(prev_watermark, now)}"


def _reason_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines = [f"Wake reasons ({len(rows)}):"]
    for row in rows:
        payload = _as_dict(row.get("payload"))
        source = str(row.get("source") or "event")
        if source == "timer":
            desc = f"timer: slept ~{payload.get('minutes')} min"
            if payload.get("reason"):
                desc += f" ({_truncate(payload['reason'], 160)})"
        else:
            desc = f"{source}: {row.get('dedup_key')}"
            detail = payload.get("summary") or payload.get("description") or ""
            if detail:
                desc += f" — {_truncate(detail, 160)}"
        lines.append(f"- {desc}")
    return lines


async def _jobs_section(
    db: Any,
    audit_reader: Any,
    project_id: Optional[str],
    prev_prints: dict[str, Any],
    prev_watermark: Optional[datetime],
    now: datetime,
) -> tuple[list[str], dict[str, Any]]:
    """Snapshot-diff of the project's jobs + audit-DB progress for active ones.

    Returns the rendered lines and the NEW fingerprints. On failure the
    PREVIOUS fingerprints are returned unchanged, so a transient query
    failure never wipes the baseline (which would replay every job as NEW on
    the next healthy sitrep).
    """
    if not project_id:
        return ["Jobs: no project bound to this officer thread."], dict(prev_prints)
    try:
        jobs = await db.get_jobs(scope_project_id=project_id, limit=200)
    except Exception:
        logger.warning("sitrep: jobs query failed", exc_info=True)
        return ["Jobs: (section unavailable — jobs query failed)"], dict(prev_prints)

    try:
        by_id = {str(j["id"]): j for j in jobs}
        active_ids = [
            jid for jid, j in by_id.items() if j.get("status") in _ACTIVE_STATUSES
        ]
        steps_by_job: dict[str, int] = {}
        if audit_reader is not None and active_ids:
            try:
                steps_by_job = await audit_reader.get_audit_counts(active_ids)
            except Exception:
                logger.warning("sitrep: audit step counts failed", exc_info=True)

        changed: list[str] = []
        new: list[str] = []
        for jid, job in by_id.items():
            status = str(job.get("status") or "unknown")
            desc = _truncate(job.get("description") or "", _DESC_CHARS)
            prev_entry = _as_dict(prev_prints.get(jid))
            if jid not in prev_prints:
                new.append(f"- NEW {jid[:8]} {status} — {desc}")
            elif prev_entry.get("status") != status:
                line = f"- {jid[:8]} {prev_entry.get('status')} → {status} — {desc}"
                error = (job.get("error_message") or "").strip()
                if error and status in ("failed", "cancelled"):
                    line += f" | error: {_truncate(error, 160)}"
                changed.append(line)

        active_lines: list[str] = []
        for jid in active_ids[:_MAX_ACTIVE]:
            job = by_id[jid]
            steps = steps_by_job.get(jid)
            prev_steps = _as_dict(prev_prints.get(jid)).get("steps")
            detail = f"- {jid[:8]} {job.get('status')}"
            if steps is not None:
                if isinstance(prev_steps, int):
                    detail += f" — steps {prev_steps}→{steps}"
                    if steps == prev_steps and prev_watermark is not None:
                        detail += " (NO PROGRESS since last sitrep)"
                else:
                    detail += f" — steps {steps}"
            last_write = None
            if audit_reader is not None:
                try:
                    time_range = await audit_reader.get_audit_time_range(jid)
                    last_write = _parse_iso((time_range or {}).get("end"))
                except Exception:
                    last_write = None
            if last_write is not None:
                detail += f", last write {_ago(last_write, now)}"
            detail += f" — {_truncate(job.get('description') or '', _DESC_CHARS)}"
            active_lines.append(detail)

        steady: dict[str, int] = {}
        for jid, job in by_id.items():
            status = str(job.get("status") or "unknown")
            prev_entry = _as_dict(prev_prints.get(jid))
            if jid in prev_prints and prev_entry.get("status") == status:
                if status not in _ACTIVE_STATUSES:
                    steady[status] = steady.get(status, 0) + 1

        lines: list[str] = [f"Jobs ({len(jobs)} in project):"]
        if changed:
            lines.append("Transitions since last sitrep:")
            lines += changed[:_MAX_TRANSITIONS]
            if len(changed) > _MAX_TRANSITIONS:
                lines.append(f"- (+{len(changed) - _MAX_TRANSITIONS} more — list_jobs)")
        if new:
            lines += new[:_MAX_NEW]
            if len(new) > _MAX_NEW:
                lines.append(f"- (+{len(new) - _MAX_NEW} more new)")
        if active_lines:
            lines.append("In flight:")
            lines += active_lines
            if len(active_ids) > _MAX_ACTIVE:
                lines.append(f"- (+{len(active_ids) - _MAX_ACTIVE} more in flight)")
        if steady:
            summary = ", ".join(f"{n} {s}" for s, n in sorted(steady.items()))
            lines.append(f"Unchanged: {summary}.")
        if not (changed or new or active_lines or steady):
            lines.append("- none")

        fingerprints = {
            jid: {
                "status": str(job.get("status") or "unknown"),
                "steps": steps_by_job.get(jid),
            }
            for jid, job in by_id.items()
        }
        return lines, fingerprints
    except Exception:
        logger.warning("sitrep: jobs diff failed", exc_info=True)
        return ["Jobs: (section unavailable — diff failed)"], dict(prev_prints)


async def _pending_section(
    db: Any, thread_id: str, project_id: Optional[str], now: datetime
) -> list[str]:
    """Decisions waiting on the officer: sudo requests + session permission
    gates. Sudo rows expire in minutes (300s TTL) — surfacing them first is
    the point of the section."""
    lines: list[str] = []
    try:
        if project_id:
            async with db.acquire() as conn:
                sudo_rows = await conn.fetch(
                    """
                    SELECT s.id, s.job_id, s.command, s.request_type, s.expires_at
                      FROM sudo_approval_requests s
                      JOIN jobs j ON j.id = s.job_id
                     WHERE j.project_id = $1
                       AND s.status = 'pending'
                       AND s.expires_at > NOW()
                     ORDER BY s.requested_at
                     LIMIT $2
                    """,
                    UUID(project_id),
                    _MAX_PENDING,
                )
            for row in sudo_rows:
                remaining = int((row["expires_at"] - now).total_seconds())
                lines.append(
                    f"- sudo ({row['request_type']}) job {str(row['job_id'])[:8]}: "
                    f"{_truncate(row['command'], 100)} — expires in {remaining}s"
                )
    except Exception:
        logger.warning("sitrep: sudo pending query failed", exc_info=True)
        lines.append("- (sudo pending unavailable)")
    try:
        async with db.acquire() as conn:
            perm_rows = await conn.fetch(
                """
                SELECT tool_name, requested_at
                  FROM thread_permission_requests
                 WHERE thread_id = $1 AND status = 'pending'
                   AND expires_at > NOW()
                 ORDER BY requested_at
                 LIMIT $2
                """,
                UUID(thread_id),
                _MAX_PENDING,
            )
        for row in perm_rows:
            lines.append(
                f"- permission gate: {row['tool_name']} "
                f"({_ago(row['requested_at'], now)})"
            )
    except Exception:
        logger.warning("sitrep: permission pending query failed", exc_info=True)
        lines.append("- (permission pending unavailable)")
    if not lines:
        return []
    return ["Pending on you:"] + lines


async def _capacity_section(
    db: Any, thread: dict[str, Any], thread_id: str
) -> list[str]:
    try:
        from services.officer_slots import capacity_lines

        metadata = _as_dict(thread.get("metadata"))
        officer_meta = _as_dict(
            _as_dict(metadata.get("config_override")).get("officer")
        )
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT context->>'officer_slot' AS slot, COUNT(*) AS n
                  FROM jobs
                 WHERE created_by_thread_id = $1
                   AND status IN ('created', 'processing')
                 GROUP BY 1
                """,
                UUID(thread_id),
            )
        in_flight_by_slot = {r["slot"]: int(r["n"]) for r in rows}
        return [capacity_lines(officer_meta, in_flight_by_slot)]
    except Exception:
        logger.warning("sitrep: capacity query failed", exc_info=True)
        return ["Capacity: (unavailable)"]


async def _fleet_section(db: Any) -> list[str]:
    try:
        async with db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, COUNT(*) AS n FROM agents GROUP BY status"
            )
        if not rows:
            return ["Fleet: no agents registered."]
        parts = ", ".join(f"{r['n']} {r['status']}" for r in rows)
        return [f"Fleet: {parts}."]
    except Exception:
        logger.warning("sitrep: fleet query failed", exc_info=True)
        return ["Fleet: (unavailable)"]


async def _budget_section(
    usage_ledger: Any, project_id: Optional[str], now: datetime
) -> list[str]:
    if usage_ledger is None or not project_id:
        return []
    try:
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        usage = await usage_ledger.query_usage(
            from_ts=day_start, to_ts=now, scope_project_id=project_id
        )
        tokens = sum(
            item.get("quantity") or 0
            for item in usage.get("by_category") or []
            if item.get("unit") == "tokens"
        )
        cost = usage.get("total_cost_usd") or 0.0
        return [f"Budget today (project): ${cost:.2f}, {int(tokens):,} tokens."]
    except Exception:
        logger.warning("sitrep: budget query failed", exc_info=True)
        return ["Budget today: (unavailable)"]
