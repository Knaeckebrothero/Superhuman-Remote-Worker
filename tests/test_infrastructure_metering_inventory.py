from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from orchestrator.services.infrastructure_metering.inventory import (
    InventoryContractError,
    InventoryItem,
    InventoryPurgeResult,
    InventoryScopeIdentity,
    InventoryStore,
    SanitizedInventoryError,
    ShadowComparison,
    ShadowComparisonStatus,
    SnapshotFinalization,
    TransportNonceClaim,
    WatchEventKind,
    WatchObjectEvent,
    canonical_request_digest,
    inventory_manifest_digest,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


def _valid(uid: str, revision: str = "a" * 64) -> InventoryItem:
    return InventoryItem(
        source_kind="pod",
        source_uid=uid,
        revision_hash=revision,
        normalized_item={"source_kind": "pod", "namespace": "workers", "uid": uid},
        valid_for_metering=True,
    )


def test_manifest_digest_has_fixed_framed_vector_and_invalid_uid_presence() -> None:
    invalid = InventoryItem(
        source_kind="pod",
        source_uid="uid-a",
        revision_hash=None,
        normalized_item={"namespace": "ns"},
        valid_for_metering=False,
        item_error=SanitizedInventoryError("capacity-invalid"),
    )
    valid = InventoryItem(
        source_kind="pod",
        source_uid="uid-b",
        revision_hash="b" * 64,
        normalized_item={"namespace": "ns"},
        valid_for_metering=True,
    )

    expected = "0746ae7268e0808061e239683c584adf7fd5364f1aaa02b1740b377e8d29da16"
    assert inventory_manifest_digest([valid, invalid]) == expected
    assert inventory_manifest_digest([invalid, valid]) == expected
    with pytest.raises(InventoryContractError, match="duplicate"):
        inventory_manifest_digest([valid, valid])


def test_request_and_transport_claims_are_canonical_and_typed() -> None:
    assert canonical_request_digest({"b": 2, "a": 1}) == canonical_request_digest(
        {"a": 1, "b": 2}
    )
    claim = TransportNonceClaim(
        collector_id="kubernetes",
        request_nonce=uuid4(),
        request_kind="watch-event",
        request_digest="a" * 64,
    )
    assert claim.request_kind == "watch-event"
    with pytest.raises(InventoryContractError, match="request_kind"):
        TransportNonceClaim(
            collector_id="kubernetes",
            request_nonce=uuid4(),
            request_kind="WATCH EVENT",
            request_digest="a" * 64,
        )


def test_watch_events_require_positive_bounded_identity_evidence() -> None:
    with pytest.raises(InventoryContractError, match="positive"):
        WatchObjectEvent(
            event_type=WatchEventKind.BOOKMARK,
            resource_version="rv-1",
            collector_observed_at=NOW,
            event_bytes=0,
        )
    with pytest.raises(InventoryContractError, match="requires item"):
        WatchObjectEvent(
            event_type=WatchEventKind.MODIFIED,
            resource_version="rv-1",
            collector_observed_at=NOW,
            event_bytes=1,
        )
    deleted = WatchObjectEvent(
        event_type=WatchEventKind.DELETED,
        resource_version="opaque-z",
        collector_observed_at=NOW,
        event_bytes=1,
        source_kind="pod",
        source_uid="uid-1",
    )
    assert deleted.identity == ("pod", "uid-1")


def test_scope_and_snapshot_contracts_do_not_accept_caller_receipt_time() -> None:
    scope = InventoryScopeIdentity(
        collector_id="kubernetes",
        source_cluster="cluster-a",
        api_resource="core/v1/pods",
        namespace="workers",
    )
    assert scope.namespace == "workers"
    final = SnapshotFinalization(
        collection_completed_at=NOW,
        complete=True,
        item_count=1,
        item_digest=inventory_manifest_digest([_valid("uid-1")]),
        resource_version="opaque-rv",
    )
    assert not hasattr(final, "received_at")


def test_store_rejects_unbounded_snapshot_and_watch_configuration() -> None:
    with pytest.raises(ValueError, match="max_batch_bytes"):
        InventoryStore(object(), max_batch_bytes=2, max_snapshot_bytes=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_watch_event_bytes"):
        InventoryStore(object(), max_watch_event_bytes=2, max_watch_bytes=1)  # type: ignore[arg-type]


def test_shadow_comparison_preserves_and_validates_lifetime_evidence() -> None:
    legacy_started_at = NOW - timedelta(seconds=5)
    comparison = ShadowComparison(
        source_uid="pod-1",
        status=ShadowComparisonStatus.LIFETIME_MISMATCH,
        reason_code="start-semantics",
        explained=False,
        comparison_at=NOW,
        owner_trusted=True,
        owner_kind="job",
        owner_id=uuid4(),
        legacy_interval_id=7,
        legacy_cpu_millicores=500,
        legacy_memory_bytes=1024,
        legacy_started_at=legacy_started_at,
        observed_cpu_millicores=500,
        observed_memory_bytes=1024,
        observed_started_at=NOW,
        observed_start_time_source="app-db-received",
        observed_start_uncertainty_us=5_000_000,
        start_delta_us=5_000_000,
    )

    payload = comparison.payload()
    assert payload["legacy_started_at"] == legacy_started_at.isoformat()
    assert payload["observed_started_at"] == NOW.isoformat()
    assert payload["start_delta_us"] == 5_000_000

    with pytest.raises(InventoryContractError, match="remain unexplained"):
        ShadowComparison(
            source_uid="pod-1",
            status=ShadowComparisonStatus.LIFETIME_MISMATCH,
            reason_code="start-semantics",
            explained=True,
            comparison_at=NOW,
        )


@pytest.mark.asyncio
async def test_diagnostic_purge_contract_enforces_floors_before_database_use() -> None:
    store = InventoryStore(object())  # type: ignore[arg-type]

    with pytest.raises(InventoryContractError, match="seven days"):
        await store.purge_diagnostics(
            1,
            snapshot_item_retention=timedelta(days=6),
        )
    with pytest.raises(InventoryContractError, match="shorter than snapshot"):
        await store.purge_diagnostics(
            1,
            snapshot_item_retention=timedelta(days=8),
            diagnostic_retention=timedelta(days=7),
        )
    with pytest.raises(InventoryContractError, match="24 hours"):
        await store.purge_diagnostics(
            1,
            abandoned_staging_retention=timedelta(hours=23),
        )

    result = InventoryPurgeResult(
        leader_generation=4,
        batch_limit=2,
        sealed_snapshots_expired=1,
        abandoned_snapshots_expired=0,
        snapshot_items_deleted=2,
        shadow_comparisons_deleted=0,
        watch_events_deleted=0,
        watch_sessions_deleted=0,
        unbound_tickets_deleted=0,
    )
    assert result.made_progress
    assert result.might_have_more
