"""Pure predicates guarding the dispatcher's preemption decisions.

Extracted from ``_try_dispatch_pending_jobs`` (orchestrator/main.py) so the
decision logic is unit-testable without standing up the whole dispatcher (which
is otherwise untested). The dispatcher performs the DB I/O and passes the
resolved values in.

See docs/done/preemption_before_first_checkpoint_replays_job_opening.md.
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


# VM provisioning decision outcomes — the dispatcher performs the I/O each names.
VM_PROVISION = "provision"  # (re)create the VM (retries remain)
VM_PARK_EXHAUSTED = "park_exhausted"  # retries used up → mark failed + park
VM_PARKED = "parked"  # already 'failed' → leave parked (no hot-retry)
VM_WAIT = "wait"  # provisioning/creating/deleting in flight → wait
VM_RECYCLE = "recycle"  # stuck past budget → tear down so it re-provisions
VM_READY = "ready"  # VM booted → proceed to claim/dispatch


def vm_provisioning_decision(
    vm_ctx: dict[str, Any],
    *,
    provision_attempts: int,
    max_provision_attempts: int,
    now: float,
    timeout_s: float,
) -> str:
    """Decide what the dispatcher should do with a VM-backed job's VM.

    Pure branch logic extracted from ``_try_dispatch_pending_jobs`` so the VM
    provisioning state machine is testable without the whole dispatcher. The
    dispatcher resolves ``vm_ctx`` / the attempt counter / the clock and performs
    the resulting I/O (create, delete, park, or dispatch).

    The attempt counter is the durable park signal: a status-based park ('failed')
    can be clobbered by the external VM controller's async status callbacks, but
    ``provision_attempts`` is monotonic in ``context.vm`` (reset to 0 only once the
    VM reaches 'ready'), so retries stay bounded regardless of callback races.

    States:
      absent / 'deleted' → PROVISION, or PARK_EXHAUSTED once retries are used up
      'failed'           → PARKED (don't hot-retry the shared VM cluster)
      'deleting'         → WAIT (teardown in flight)
      not-yet-'ready'    → RECYCLE if stuck past ``timeout_s``, else WAIT
      'ready'            → READY (proceed to claim)
    """
    status = vm_ctx.get("status")
    if not status or status == "deleted":
        if provision_attempts >= max_provision_attempts:
            return VM_PARK_EXHAUSTED
        return VM_PROVISION
    if status == "failed":
        return VM_PARKED
    if status == "deleting":
        return VM_WAIT
    if status != "ready":
        provisioned_at = vm_ctx.get("provisioned_at")
        if provisioned_at and (now - float(provisioned_at)) > timeout_s:
            return VM_RECYCLE
        return VM_WAIT
    return VM_READY
