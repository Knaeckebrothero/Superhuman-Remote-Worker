"""Durable provisioning boundary for Officer-created ticket jobs.

Officer admission owns the ticket claim and capacity reservation.  This module
owns the narrower transition from a born-paused, dispatcher-invisible job to a
normal ``created`` job after every mandatory provisioning step has completed.
The jobs row is both the durable state machine and the recovery lease; no new
public job status is required.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

PREFLIGHT_FREEZE_TYPE = "officer_preflight"
PREFLIGHT_STATES = frozenset(
    {
        "not-attempted",
        "in-progress",
        "retryable-failed",
        "permanent-failed",
        "activated",
    }
)

ProvisionFn = Callable[..., Awaitable[None]]


class OfficerProvisioningError(RuntimeError):
    """Classified mandatory provisioning failure."""

    def __init__(
        self,
        message: str,
        *,
        phase: str = "repository",
        retryable: bool = True,
        failure_class: str = "infrastructure",
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.retryable = retryable
        self.failure_class = failure_class


@dataclass(frozen=True, slots=True)
class OfficerPreflightOutcome:
    job_id: str
    state: str
    activated: bool
    attempted: bool
    retryable: bool | None = None
    phase: str | None = None
    error: str | None = None


def initial_preflight_context(*, category: str | None = None) -> dict[str, Any]:
    """Server-authored initial state stamped inside Officer admission."""

    return {
        "required": True,
        "state": "not-attempted",
        "attempts": 0,
        "category": category,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def initial_preflight_freeze() -> dict[str, Any]:
    """Dispatcher barrier stored atomically with the admitted job."""

    return {
        "freeze_type": PREFLIGHT_FREEZE_TYPE,
        "reason": "mandatory Officer job provisioning has not activated",
    }


def preflight_state(job: dict[str, Any] | None) -> str | None:
    context = (job or {}).get("context") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (TypeError, ValueError):
            context = {}
    if not isinstance(context, dict):
        return None
    value = (context.get("provisioning_preflight") or {}).get("state")
    return str(value) if value in PREFLIGHT_STATES else None


async def ensure_officer_job_activated(
    db: Any,
    job: dict[str, Any],
    *,
    provision: ProvisionFn | None,
    category: str | None = None,
    trigger_dispatch: Callable[[], None] | None = None,
    lease_seconds: int = 300,
    retry_after_seconds: int = 60,
    fault_injector: Callable[[str], Any] | None = None,
) -> OfficerPreflightOutcome:
    """Provision and activate one strict Officer job under a durable lease.

    Faults named ``after_provisioning_before_activation`` and
    ``after_activation`` deliberately escape without being converted to a
    provisioning failure.  They model process loss at the two transaction
    edges: recovery may reclaim the expired first lease, while the second edge
    is already durably activated and therefore cannot provision twice.
    """

    job_id = str(job.get("id") or "")
    if not job_id:
        raise ValueError("Officer preflight requires a durable job id")

    async def _fault(step: str) -> None:
        if fault_injector is None:
            return
        result = fault_injector(step)
        if hasattr(result, "__await__"):
            await result

    claimed = await db.claim_officer_job_preflight(job_id, lease_seconds=lease_seconds)
    if claimed is None:
        current = await db.get_job(job_id)
        state = preflight_state(current) or preflight_state(job) or "not-attempted"
        return OfficerPreflightOutcome(
            job_id=job_id,
            state=state,
            activated=state == "activated",
            attempted=False,
        )

    token = str(claimed["preflight_attempt_token"])
    effective_category = category
    if effective_category is None:
        context = claimed.get("context") or {}
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except (TypeError, ValueError):
                context = {}
        preflight = context.get("provisioning_preflight") or {}
        effective_category = preflight.get("category") or context.get("work_category")

    try:
        if provision is None:
            raise OfficerProvisioningError(
                "mandatory provisioning service is unavailable",
                phase="repository",
                retryable=True,
            )
        await provision(claimed, category=effective_category)
    except Exception as exc:
        phase = str(getattr(exc, "phase", "repository"))
        retryable = bool(getattr(exc, "retryable", True))
        failure_class = str(getattr(exc, "failure_class", "infrastructure"))
        error = str(exc)[:1000] or exc.__class__.__name__
        recorded = await db.finish_officer_job_preflight(
            job_id,
            attempt_token=token,
            activated=False,
            retryable=retryable,
            phase=phase,
            failure_class=failure_class,
            error=error,
            retry_after_seconds=retry_after_seconds,
        )
        if not recorded:
            logger.warning("Officer preflight failure CAS lost for job %s", job_id[:8])
        return OfficerPreflightOutcome(
            job_id=job_id,
            state="retryable-failed" if retryable else "permanent-failed",
            activated=False,
            attempted=True,
            retryable=retryable,
            phase=phase,
            error=error,
        )

    await _fault("after_provisioning_before_activation")
    activated = await db.finish_officer_job_preflight(
        job_id,
        attempt_token=token,
        activated=True,
        phase="complete",
    )
    if not activated:
        current = await db.get_job(job_id)
        state = preflight_state(current) or "in-progress"
        return OfficerPreflightOutcome(
            job_id=job_id,
            state=state,
            activated=state == "activated",
            attempted=True,
        )

    await _fault("after_activation")
    if trigger_dispatch is not None:
        try:
            trigger_dispatch()
        except Exception:
            logger.exception(
                "Officer preflight dispatch nudge failed for job %s", job_id[:8]
            )
    return OfficerPreflightOutcome(
        job_id=job_id,
        state="activated",
        activated=True,
        attempted=True,
    )


__all__ = [
    "OfficerPreflightOutcome",
    "OfficerProvisioningError",
    "PREFLIGHT_FREEZE_TYPE",
    "PREFLIGHT_STATES",
    "ensure_officer_job_activated",
    "initial_preflight_context",
    "initial_preflight_freeze",
    "preflight_state",
]
