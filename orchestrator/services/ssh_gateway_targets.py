"""Resolve a session's workspace liveness for the SSH gateway.

Liveness here is THREE independent axes, and conflating them produces a
resolver that is wrong in the most common case:

  session   threads.status
  lane      threads.execution_lane plus its retirement marker
  workspace workspace_container.status / vm.status

An *ended* session routinely still has a *ready* workspace until the idle
sweeper fires — that sweeper's own query keys on exactly that combination.
Refusing SSH there is correct, but the reason we report must come from the
session axis while the workspace fields are derived independently, or the
negative controls become untestable.
"""

from __future__ import annotations

from typing import Any

STATE_LIVE = "live"
STATE_ENDING = "ending"
STATE_SUSPENDED = "suspended"
STATE_RECLAIMED = "reclaimed"
STATE_RESTORING = "restoring"
STATE_ENDED = "ended"
STATE_NEVER_PROVISIONED = "never_provisioned"
STATE_VM_UNSUPPORTED = "vm_unsupported"
# Not returned by resolve_workspace_state() itself — the endpoint reports
# this when workspace_container.status IS "ready" (genuinely provisioned)
# but resolve_remote_workspace_target() raises CanvasSSHError: a missing/
# unattested _workspace_binding, a stale generation, or a malformed SSH
# identity. Collapsing that into STATE_NEVER_PROVISIONED would send an
# operator after the wrong problem — the workspace exists, its SSH
# attestation doesn't. Defined here (not inline in main.py) so it shares
# the same import/reuse path as the other STATE_* constants.
STATE_STALE_BINDING = "stale_binding"


def resolve_workspace_state(thread: dict[str, Any], metadata: dict[str, Any]) -> str:
    """Collapse the three axes into the single state the gateway reports.

    ``metadata`` MUST already be parsed — asyncpg returns JSONB as ``str``.
    """
    if (thread.get("status") or "") == "ended":
        return STATE_ENDED

    lane = thread.get("execution_lane") or "pinned"
    if lane == "pinned":
        if thread.get("runtime_retirement_token"):
            return STATE_ENDING
    elif metadata.get("_stateless_workspace_retirement_pending") or metadata.get(
        "_stateless_claim_retirement"
    ):
        return STATE_ENDING

    container = metadata.get("workspace_container") or {}
    # Not "is the key absent": _setup_gitea writes git_remote_url/repo_name onto
    # every thread regardless of tier, so the key is always there. Absence of a
    # *status* is what means never provisioned.
    status = container.get("status")
    if not status:
        return STATE_NEVER_PROVISIONED

    if status == "suspended":
        return STATE_RECLAIMED if container.get("volume_reclaimed") else STATE_SUSPENDED
    if status in {"restoring", "creating", "created", "pending", "suspending"}:
        return STATE_RESTORING
    if status == "ready":
        return STATE_LIVE
    return STATE_NEVER_PROVISIONED
