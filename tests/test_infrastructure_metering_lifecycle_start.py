"""Shared Slice 3 compute lifecycle-start evidence contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from orchestrator.services.infrastructure_metering.inventory import (
    InventoryConflictError,
    InventoryItem,
    WatchEventKind,
    WatchIntervalMutationContext,
)
from orchestrator.services.infrastructure_metering.lifecycle_start import (
    receipt_lifecycle_start,
    watch_lifecycle_start,
)


RECEIVED_AT = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)


def _item(source_kind: str = "pod") -> InventoryItem:
    return InventoryItem(
        source_kind=source_kind,
        source_uid=f"{source_kind}-uid-1",
        revision_hash="a" * 64,
        valid_for_metering=True,
        normalized_item={"lifecycle": {"accrues": True}},
    )


def _context(event_type: WatchEventKind) -> WatchIntervalMutationContext:
    return WatchIntervalMutationContext(
        scope_epoch_id=uuid4(),
        inventory_scope_id=uuid4(),
        source_cluster="cluster-a",
        namespace="srw",
        event_type=event_type,
        received_at=RECEIVED_AT,
        existing_interval_id=None,
        existing_source_revision=None,
    )


def test_receipt_start_records_uncertainty_from_sane_scheduled_evidence():
    start = receipt_lifecycle_start(
        received_at=RECEIVED_AT,
        authority_boundary=RECEIVED_AT - timedelta(minutes=10),
        creation_at=RECEIVED_AT - timedelta(minutes=8),
        scheduled_at=RECEIVED_AT - timedelta(minutes=5),
        scheduled_source="pod-scheduled-transition",
    )

    assert start.started_at == RECEIVED_AT
    assert start.time_source == "app-db-received"
    assert start.uncertainty_us == 5 * 60 * 1_000_000
    assert start.evidence_source == "pod-scheduled-transition"


def test_receipt_start_clamps_uncertainty_to_compute_authority():
    boundary = RECEIVED_AT - timedelta(minutes=2)
    start = receipt_lifecycle_start(
        received_at=RECEIVED_AT,
        authority_boundary=boundary,
        creation_at=RECEIVED_AT - timedelta(hours=1),
        scheduled_at=RECEIVED_AT - timedelta(minutes=30),
        scheduled_source="vmi-scheduled-transition",
    )

    assert start.started_at == RECEIVED_AT
    assert start.uncertainty_us == 2 * 60 * 1_000_000
    assert start.evidence_source.endswith("-clamped-to-compute-authority")


def test_receipt_start_rejects_a_queued_pre_authority_receipt():
    with pytest.raises(InventoryConflictError, match="precedes its authority"):
        receipt_lifecycle_start(
            received_at=RECEIVED_AT,
            authority_boundary=RECEIVED_AT + timedelta(microseconds=1),
            creation_at=None,
            scheduled_at=None,
            scheduled_source="pod-scheduled-transition",
        )


@pytest.mark.asyncio
async def test_added_watch_admits_transition_after_complete_absence_proof():
    context = _context(WatchEventKind.ADDED)
    transition = RECEIVED_AT - timedelta(seconds=5)
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {
            "continuity_health": "healthy",
            "continuous_since": RECEIVED_AT - timedelta(minutes=2),
            "snapshot_id": uuid4(),
            "proof_at": RECEIVED_AT - timedelta(seconds=10),
            "complete": True,
            "manifest_state": "sealed",
        },
        None,
    ]
    conn.fetchval.return_value = None

    start = await watch_lifecycle_start(
        conn,
        context=context,
        item=_item(),
        source_kind="pod",
        authority_boundary=RECEIVED_AT - timedelta(minutes=1),
        creation_at=RECEIVED_AT - timedelta(minutes=1),
        scheduled_at=transition,
        scheduled_source="pod-scheduled-transition",
    )

    assert start.started_at == transition
    assert start.time_source == "pod-scheduled-transition"
    assert start.uncertainty_us == 0
    assert start.evidence_source == "continuous-watch-proof"


@pytest.mark.asyncio
async def test_watch_without_continuity_uses_receipt_and_uncertainty():
    context = _context(WatchEventKind.ADDED)
    transition = RECEIVED_AT - timedelta(seconds=5)
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "continuity_health": "gap",
        "continuous_since": None,
        "snapshot_id": None,
        "proof_at": None,
        "complete": False,
        "manifest_state": "staging",
    }

    start = await watch_lifecycle_start(
        conn,
        context=context,
        item=_item(),
        source_kind="pod",
        authority_boundary=RECEIVED_AT - timedelta(minutes=1),
        creation_at=RECEIVED_AT - timedelta(minutes=1),
        scheduled_at=transition,
        scheduled_source="pod-scheduled-transition",
    )

    assert start.started_at == RECEIVED_AT
    assert start.time_source == "app-db-received"
    assert start.uncertainty_us == 5_000_000


@pytest.mark.asyncio
async def test_remote_transition_outside_clock_skew_is_never_backdated():
    context = _context(WatchEventKind.ADDED)
    conn = AsyncMock()

    start = await watch_lifecycle_start(
        conn,
        context=context,
        item=_item("vmi"),
        source_kind="vmi",
        authority_boundary=RECEIVED_AT - timedelta(hours=1),
        creation_at=RECEIVED_AT - timedelta(minutes=10),
        scheduled_at=RECEIVED_AT - timedelta(minutes=6),
        scheduled_source="vmi-scheduled-transition",
        max_scheduled_clock_skew=timedelta(minutes=5),
    )

    assert start.started_at == RECEIVED_AT
    assert start.time_source == "app-db-received"
    assert start.evidence_source == "object-creation-timestamp"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_proven_transition_is_clamped_to_exact_authority_boundary():
    context = _context(WatchEventKind.MODIFIED)
    boundary = RECEIVED_AT - timedelta(seconds=3)
    conn = AsyncMock()
    conn.fetchrow.side_effect = [
        {
            "continuity_health": "healthy",
            "continuous_since": RECEIVED_AT - timedelta(minutes=2),
            "snapshot_id": uuid4(),
            "proof_at": RECEIVED_AT - timedelta(seconds=10),
            "complete": True,
            "manifest_state": "sealed",
        },
        {
            "event_type": "modified",
            "received_at": RECEIVED_AT - timedelta(seconds=10),
            "prior_accrues": "false",
        },
    ]

    start = await watch_lifecycle_start(
        conn,
        context=context,
        item=_item("vmi"),
        source_kind="vmi",
        authority_boundary=boundary,
        creation_at=RECEIVED_AT - timedelta(minutes=1),
        scheduled_at=RECEIVED_AT - timedelta(seconds=5),
        scheduled_source="vmi-scheduled-transition",
    )

    assert start.started_at == boundary
    assert start.time_source == "compute-authority-boundary"
    assert start.uncertainty_us == 0
    assert start.evidence_source == ("continuous-watch-proof:vmi-scheduled-transition")
