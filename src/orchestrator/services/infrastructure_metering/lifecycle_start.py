"""Conservative compute-lifecycle start selection.

LIST receipt time is the accounting authority unless a healthy, continuous
WATCH history proves an object's scheduled transition.  Kubernetes timestamps
remain bounded evidence: they may widen uncertainty, but a delayed initial
observation cannot retroactively manufacture an exact billable interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from orchestrator.services.infrastructure_metering.inventory import (
    InventoryConflictError,
    InventoryItem,
    WatchIntervalMutationContext,
)


@dataclass(frozen=True, slots=True)
class LifecycleStart:
    """One admitted interval start plus its immutable evidence metadata."""

    started_at: datetime
    time_source: str
    uncertainty_us: int
    evidence_source: str


def parse_lifecycle_timestamp(value: Any) -> datetime | None:
    """Parse one normalized ISO-8601 instant without accepting naive time."""

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError):
        return None
    return _utc(parsed)


def _utc(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _microseconds_between(later: datetime, earlier: datetime) -> int:
    delta = later - earlier
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def receipt_lifecycle_start(
    *,
    received_at: datetime,
    authority_boundary: datetime,
    creation_at: datetime | None,
    scheduled_at: datetime | None,
    status_start_at: datetime | None = None,
    scheduled_source: str,
    max_scheduled_clock_skew: timedelta | None = None,
) -> LifecycleStart:
    """Use receipt time while retaining the best safe lower-bound evidence."""

    receipt = _utc(received_at)
    authority = _utc(authority_boundary)
    if receipt is None or authority is None:
        raise InventoryConflictError("compute lifecycle timestamps must be UTC-aware")
    if receipt < authority:
        raise InventoryConflictError("compute observation precedes its authority")

    if max_scheduled_clock_skew is not None and max_scheduled_clock_skew < timedelta(0):
        raise ValueError("scheduled lifecycle clock skew must be nonnegative")
    creation = _utc(creation_at)
    scheduled = _utc(scheduled_at)
    if (
        scheduled is not None
        and max_scheduled_clock_skew is not None
        and abs(receipt - scheduled) > max_scheduled_clock_skew
    ):
        scheduled = None
    evidence_at: datetime | None = None
    evidence_source: str | None = None
    for candidate, source in (
        (scheduled, scheduled_source),
        (_utc(status_start_at), "pod-status-start-time"),
        (creation, "object-creation-timestamp"),
    ):
        if candidate is None or candidate > receipt:
            continue
        if creation is not None and candidate < creation:
            continue
        evidence_at = candidate
        evidence_source = source
        break

    if evidence_at is None:
        evidence_at = authority
        evidence_source = "compute-authority-boundary"
    elif evidence_at < authority:
        evidence_at = authority
        evidence_source = f"{evidence_source}-clamped-to-compute-authority"

    return LifecycleStart(
        started_at=receipt,
        time_source="app-db-received",
        uncertainty_us=_microseconds_between(receipt, evidence_at),
        evidence_source=evidence_source,
    )


async def watch_lifecycle_start(
    conn: asyncpg.Connection,
    *,
    context: WatchIntervalMutationContext,
    item: InventoryItem,
    source_kind: str,
    authority_boundary: datetime,
    creation_at: datetime | None,
    scheduled_at: datetime | None,
    status_start_at: datetime | None = None,
    scheduled_source: str,
    max_scheduled_clock_skew: timedelta | None = None,
) -> LifecycleStart:
    """Admit a scheduled transition only across proven continuous history."""

    fallback = receipt_lifecycle_start(
        received_at=context.received_at,
        authority_boundary=authority_boundary,
        creation_at=creation_at,
        scheduled_at=scheduled_at,
        status_start_at=status_start_at,
        scheduled_source=scheduled_source,
        max_scheduled_clock_skew=max_scheduled_clock_skew,
    )
    transition = _utc(scheduled_at)
    creation = _utc(creation_at)
    if (
        transition is None
        or transition > context.received_at
        or (creation is not None and transition < creation)
        or (
            max_scheduled_clock_skew is not None
            and abs(context.received_at - transition) > max_scheduled_clock_skew
        )
    ):
        return fallback

    baseline = await conn.fetchrow(
        "SELECT epoch.continuity_health, epoch.continuous_since, "
        "snapshot.id AS snapshot_id, snapshot.received_at AS proof_at, "
        "snapshot.complete, snapshot.manifest_state "
        "FROM resource_inventory_scope_epochs epoch "
        "LEFT JOIN resource_inventory_snapshots snapshot "
        "ON snapshot.id=epoch.last_complete_snapshot_id "
        "AND snapshot.scope_epoch_id=epoch.id "
        "WHERE epoch.id=$1 AND epoch.scope_id=$2 AND epoch.retired_at IS NULL",
        context.scope_epoch_id,
        context.inventory_scope_id,
    )
    if (
        baseline is None
        or baseline["continuity_health"] != "healthy"
        or baseline["continuous_since"] is None
        or baseline["snapshot_id"] is None
        or baseline["complete"] is not True
        or baseline["manifest_state"] not in {"sealed", "items-expired"}
    ):
        return fallback

    prior = await conn.fetchrow(
        "SELECT event_type, received_at, "
        "COALESCE(normalized_item->'lifecycle'->>'accrues', '') "
        "AS prior_accrues "
        "FROM resource_inventory_watch_events "
        "WHERE scope_epoch_id=$1 AND source_kind=$4 AND source_uid=$2 "
        "AND received_at < $3 "
        "ORDER BY received_at DESC, ordinal DESC LIMIT 1",
        context.scope_epoch_id,
        item.source_uid,
        context.received_at,
        source_kind,
    )

    proof_at: datetime | None = None
    if context.event_type.value == "added":
        if prior is not None or baseline["manifest_state"] != "sealed":
            return fallback
        present_in_baseline = await conn.fetchval(
            "SELECT TRUE FROM resource_inventory_snapshot_items "
            "WHERE snapshot_id=$1 AND source_kind=$3 AND source_uid=$2",
            baseline["snapshot_id"],
            item.source_uid,
            source_kind,
        )
        if not present_in_baseline:
            proof_at = baseline["proof_at"]
    elif context.event_type.value == "modified":
        if prior is not None:
            if (
                prior["event_type"] not in {"added", "modified"}
                or prior["prior_accrues"] != "false"
            ):
                return fallback
            proof_at = prior["received_at"]
        else:
            if baseline["manifest_state"] != "sealed":
                return fallback
            baseline_item = await conn.fetchrow(
                "SELECT COALESCE(normalized_item->'lifecycle'->>'accrues', '') "
                "AS prior_accrues FROM resource_inventory_snapshot_items "
                "WHERE snapshot_id=$1 AND source_kind=$3 AND source_uid=$2",
                baseline["snapshot_id"],
                item.source_uid,
                source_kind,
            )
            if baseline_item is None or baseline_item["prior_accrues"] != "false":
                return fallback
            proof_at = baseline["proof_at"]

    proof = _utc(proof_at)
    continuous_since = _utc(baseline["continuous_since"])
    if (
        proof is None
        or continuous_since is None
        or proof < continuous_since
        or transition < proof
    ):
        return fallback

    authority = _utc(authority_boundary)
    if authority is None:
        raise InventoryConflictError("compute authority timestamp must be UTC-aware")
    if transition < authority:
        return LifecycleStart(
            started_at=authority,
            time_source="compute-authority-boundary",
            uncertainty_us=0,
            evidence_source=f"continuous-watch-proof:{scheduled_source}",
        )
    return LifecycleStart(
        started_at=transition,
        time_source=scheduled_source,
        uncertainty_us=0,
        evidence_source="continuous-watch-proof",
    )


__all__ = [
    "LifecycleStart",
    "parse_lifecycle_timestamp",
    "receipt_lifecycle_start",
    "watch_lifecycle_start",
]
