"""Pure predicates guarding the dispatcher's preemption decisions.

Extracted from ``_try_dispatch_pending_jobs`` (orchestrator/main.py) so the
decision logic is unit-testable without standing up the whole dispatcher (which
is otherwise untested). The dispatcher performs the DB I/O and passes the
resolved values in.

See knowledge-history/done/preemption_before_first_checkpoint_replays_job_opening.md.
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


def resume_lane_applies(job: dict[str, Any], *, has_checkpoint: bool) -> bool:
    """True → dispatch via ``/job/resume``; False → fresh ``/job/start``.

    Only a 'paused' job that actually RAN may take the resume lane. A
    paused-but-never-started job re-dispatched as a resume reaches the agent
    with no description/deliverables/kickoff — ``JobResumeRequest`` carries
    none of them — so it starts brief-less and strands. The caller resolves
    ``has_checkpoint`` (``PostgresDB.job_has_checkpoint``); when checkpoint
    presence is unknowable (sqlite backend) that probe fails open to True,
    preserving today's behavior — the agent-side brief hydration + tripwire
    cover that half. See
    knowledge-base/knowledge/issues/fresh_job_dispatched_as_resume_skips_seeding.md.
    """
    if str(job.get("status") or "") != "paused":
        return False
    return has_checkpoint


# VM provisioning decision outcomes — the dispatcher performs the I/O each names.
VM_PROVISION = "provision"  # (re)create the VM (retries remain)
VM_PARK_EXHAUSTED = "park_exhausted"  # retries used up → mark failed + park
VM_PARKED = "parked"  # already 'failed' → leave parked (no hot-retry)
VM_WAIT = "wait"  # provisioning/creating/deleting in flight → wait
VM_RECYCLE = "recycle"  # stuck past budget → tear down so it re-provisions
VM_READY = "ready"  # VM booted → proceed to claim/dispatch
VM_GOLDEN_POLL = "golden_poll"  # golden image importing → re-poll create, free
VM_PARK_GOLDEN = "park_golden"  # golden import never finished → fail + park
VM_HEADSCALE_POLL = "headscale_poll"  # mesh VPN down → re-poll create, free
VM_PARK_HEADSCALE = "park_headscale"  # mesh VPN never recovered → fail + park

# Teardown states the controller reports when a delete does not complete. Both
# used to match no branch and fall through to the generic not-ready arm, which
# RECYCLEs forever — PARK_EXHAUSTED only triggers on absent-or-'deleted', so the
# job could never reach a terminal state.
TEARDOWN_FAILED_STATUSES = ("delete_failed", "query_failed")

# Suspend/restore states. The suspension subsystem owns these transitions and
# keeps the rootdisk on purpose; the dispatcher must wait rather than treat them
# as "stuck short of ready" and tear the VM down (which purges that disk).
SUSPEND_STATUSES = ("suspending", "suspended", "restoring")

# Bounded patience for a cold golden-image import (an agent-vm-base bump).
# Observed cold import on the shared VM cluster: ~30 min — deliberately above
# both VM_PROVISION_TIMEOUT_S (600) and the controller's VM_GOLDEN_POLL_TIMEOUT
# (900), which is the misalignment that burned a loop iteration (see
# knowledge-history/done/golden_image_cold_import_fails_inflight_vm_jobs.md).
DEFAULT_GOLDEN_WAIT_TIMEOUT_S = 2700.0

# Bounded patience for a Headscale outage. The controller refuses to build a
# VM it cannot hand a tailnet key to, so this budget covers "how long might
# Headscale plausibly be down". Observed worst case: a full homelab reboot,
# where Headscale trailed the VM controller by ~8 min. 15 min leaves margin
# without stalling a loop iteration on a genuinely dead mesh.
DEFAULT_HEADSCALE_WAIT_TIMEOUT_S = 900.0


def _bounded_teardown_retry(
    provision_attempts: int, max_provision_attempts: int
) -> str:
    """Retry a failed/stuck teardown, but let the attempt budget end it.

    RECYCLE re-issues the delete; without this bound the controller's
    delete_failed → RECYCLE → delete_failed cycle never terminates.
    """
    if provision_attempts >= max_provision_attempts:
        return VM_PARK_EXHAUSTED
    return VM_RECYCLE


def vm_provisioning_decision(
    vm_ctx: dict[str, Any],
    *,
    provision_attempts: int,
    max_provision_attempts: int,
    now: float,
    timeout_s: float,
    golden_timeout_s: float = DEFAULT_GOLDEN_WAIT_TIMEOUT_S,
    headscale_timeout_s: float = DEFAULT_HEADSCALE_WAIT_TIMEOUT_S,
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
      'suspending'/'suspended'/'restoring'
                         → WAIT (the suspension subsystem owns these and keeps
                           the rootdisk deliberately; recycling would purge it)
      'deleting'         → WAIT while in flight; once stuck past ``timeout_s``
                           from ``deleting_started_at``, RECYCLE to re-issue the
                           teardown, or PARK_EXHAUSTED once retries are gone
      'delete_failed'/'query_failed'
                         → RECYCLE (re-issue), PARK_EXHAUSTED once retries are
                           gone — previously these reached no terminal state
      'waiting_golden'   → GOLDEN_POLL within ``golden_timeout_s`` of
                           ``golden_wait_started_at``, else PARK_GOLDEN. The
                           controller has NOT created a VM yet — it is waiting
                           on a shared golden-image import (a cold import after
                           an agent-vm-base bump takes ~30 min, longer than
                           ``timeout_s``). Polling re-issues create WITHOUT
                           consuming a provision attempt: the attempt budget
                           bounds VM boots, and no boot is happening. RECYCLE
                           would be meaningless here (nothing to tear down) and
                           counting attempts would park every job dispatched
                           into the import window — the exact failure this
                           branch removes.
      'waiting_headscale'→ HEADSCALE_POLL within ``headscale_timeout_s`` of
                           ``headscale_wait_started_at``, else PARK_HEADSCALE.
                           Same shape as waiting_golden and for the same
                           reason: no VM exists, so polling re-issues create
                           WITHOUT consuming a provision attempt. The
                           controller refuses to build a VM while Headscale
                           is down, because a VM with no tailnet pre-auth key
                           boots fine but is unreachable forever — it would
                           silently burn the whole attempt budget.
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
    if status in SUSPEND_STATUSES:
        return VM_WAIT
    if status == "deleting":
        # Both the delete request and the controller's answer are fire-and-forget
        # core NATS (at-most-once, no JetStream), so a dropped message strands the
        # job here. Re-issue the teardown once it is provably stuck. Rows written
        # before this stamp existed carry no start time — staleness is unknowable,
        # so they keep the old non-destructive behaviour.
        started = vm_ctx.get("deleting_started_at")
        if started and (now - float(started)) > timeout_s:
            return _bounded_teardown_retry(provision_attempts, max_provision_attempts)
        return VM_WAIT
    if status in TEARDOWN_FAILED_STATUSES:
        return _bounded_teardown_retry(provision_attempts, max_provision_attempts)
    if status == "waiting_golden":
        started = vm_ctx.get("golden_wait_started_at")
        if started and (now - float(started)) > golden_timeout_s:
            return VM_PARK_GOLDEN
        return VM_GOLDEN_POLL
    if status == "waiting_headscale":
        started = vm_ctx.get("headscale_wait_started_at")
        if started and (now - float(started)) > headscale_timeout_s:
            return VM_PARK_HEADSCALE
        return VM_HEADSCALE_POLL
    if status != "ready":
        provisioned_at = vm_ctx.get("provisioned_at")
        if provisioned_at and (now - float(provisioned_at)) > timeout_s:
            return VM_RECYCLE
        return VM_WAIT
    return VM_READY
