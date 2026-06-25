"""Pure predicates guarding the dispatcher's preemption decisions.

Extracted from ``_try_dispatch_pending_jobs`` (orchestrator/main.py) so the
decision logic is unit-testable without standing up the whole dispatcher (which
is otherwise untested). The dispatcher performs the DB I/O and passes the
resolved values in.

See docs/issues/preemption_before_first_checkpoint_replays_job_opening.md.
"""

from __future__ import annotations

from typing import Any, Optional

# Statuses from which a job can never make progress again.
_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def preemption_blocked_reason(
    pending_job: dict[str, Any], parent_status: Optional[str]
) -> Optional[str]:
    """Return why ``pending_job`` must NOT preempt a running job, or ``None``.

    A verification/critic subjob whose parent is already terminal can never make
    progress, so it must not pause healthy work to "make room" for itself — yet
    the dispatcher would still treat it as a high-priority pending job. Root jobs
    (no parent) and subjobs whose parent is still live are unaffected.

    Args:
        pending_job: the candidate preemptor job row.
        parent_status: status of ``pending_job``'s parent, or ``None`` if it has
            no parent or the parent could not be found.

    Returns:
        A short human-readable reason string if preemption must be blocked, else
        ``None`` (preemption may proceed).
    """
    if pending_job.get("parent_job_id") and parent_status in _TERMINAL_STATUSES:
        return f"subjob parent is terminal ({parent_status})"
    return None
