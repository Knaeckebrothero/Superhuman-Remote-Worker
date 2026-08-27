"""Safe presentation helpers for server-owned terminal job outcomes."""

from __future__ import annotations

from typing import Any, Mapping

BLOCKED_UNDELIVERED = "blocked_undelivered"


def effective_job_status(job: Mapping[str, Any], *, fallback: str = "unknown") -> str:
    """Return the user-facing outcome without changing storage status."""

    if job.get("completion_outcome_kind") == BLOCKED_UNDELIVERED:
        return BLOCKED_UNDELIVERED
    return str(job.get("status") or fallback)


__all__ = ["BLOCKED_UNDELIVERED", "effective_job_status"]
