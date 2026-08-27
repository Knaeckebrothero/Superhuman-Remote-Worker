from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from orchestrator.services.infrastructure_metering.inventory import (
    _RECONCILIATION_COUNTS_SQL,
    InventoryConflictError,
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


def test_pending_items_only_exclude_explicit_not_applicable_comparisons() -> None:
    compact = " ".join(_RECONCILIATION_COUNTS_SQL.split())

    assert "LEFT JOIN resource_inventory_shadow_comparisons" in compact
    assert "comparison.status IS NULL" in compact
    assert "comparison.status <> 'not-applicable'" in compact


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


class _AsyncContext:
    def __init__(self, value=None) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


def _dark_watch_store():
    session_id = uuid4()
    epoch_id = uuid4()
    scope_id = uuid4()
    event_id = uuid4()
    session = {
        "id": session_id,
        "scope_epoch_id": epoch_id,
        "leader_generation": 7,
        "last_resource_version": "rv-1",
        "committed_events": 0,
        "committed_bytes": 0,
        "max_events": 100,
        "max_bytes": 1_000_000,
        "consumed_at": None,
    }
    epoch = {
        "id": epoch_id,
        "scope_id": scope_id,
        "source_cluster": "cluster-a",
        "namespace": "workers",
        "collector_id": "kubernetes",
        "last_resource_version": "rv-1",
    }
    stored = {
        "id": event_id,
        "event_type": "modified",
        "resource_version": "rv-2",
        "expected_resource_version": "rv-1",
        "mutation_action": "not-applicable",
        "received_at": NOW,
        "affected_interval_id": None,
        "coverage_gap_id": None,
    }
    conn = SimpleNamespace()

    async def fetchrow(sql, *_args):
        if "INSERT INTO resource_inventory_watch_events" in sql:
            return stored
        if "FROM resource_inventory_watch_sessions" in sql:
            return session
        if "FROM resource_inventory_watch_events" in sql:
            return None
        if "FROM resource_intervals" in sql:
            return None
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def fetchval(sql, *_args):
        if "statement_timestamp" in sql:
            return NOW
        if "SELECT TRUE FROM resource_intervals" in sql:
            return False
        if "UPDATE resource_inventory_scope_epochs" in sql:
            return True
        raise AssertionError(f"unexpected fetchval SQL: {sql}")

    conn.fetchrow = AsyncMock(side_effect=fetchrow)
    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.execute = AsyncMock()
    conn.transaction = lambda: _AsyncContext()
    pool = SimpleNamespace(acquire=lambda: _AsyncContext(conn))
    store = InventoryStore(pool)
    store._lock_watch_session = AsyncMock(return_value=(session, epoch))
    store._claim_transport_nonce = AsyncMock()
    scope = InventoryScopeIdentity(
        collector_id="kubernetes",
        source_cluster="cluster-a",
        api_resource="core/v1/pods",
        namespace="workers",
    )
    transport = TransportNonceClaim(
        collector_id="kubernetes",
        request_nonce=uuid4(),
        request_kind="watch-event",
        request_digest="a" * 64,
    )
    event = WatchObjectEvent(
        event_type=WatchEventKind.MODIFIED,
        resource_version="rv-2",
        collector_observed_at=NOW,
        event_bytes=10,
        item=_valid("uid-1"),
    )
    return store, conn, session_id, event_id, scope, transport, event


@pytest.mark.asyncio
async def test_inventory_only_watch_runs_dark_hook_without_interval_sql() -> None:
    store, conn, session_id, event_id, scope, transport, event = _dark_watch_store()
    mutator = AsyncMock(return_value=None)

    result = await store.apply_watch_event(
        "t" * 32,
        session_id,
        event_id,
        "a" * 64,
        "rv-1",
        event,
        scope=scope,
        transport=transport,
        interval_mutator=mutator,
        reconcile_intervals=False,
    )

    assert result.mutation_action.value == "not-applicable"
    mutator.assert_awaited_once()
    assert mutator.await_args.args[1].existing_interval_id is None
    assert not any(
        "UPDATE resource_intervals" in str(call.args[0])
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_inventory_only_watch_rejects_mutator_returned_interval() -> None:
    store, _conn, session_id, event_id, scope, transport, event = _dark_watch_store()

    with pytest.raises(InventoryConflictError, match="returned an interval"):
        await store.apply_watch_event(
            "t" * 32,
            session_id,
            event_id,
            "a" * 64,
            "rv-1",
            event,
            scope=scope,
            transport=transport,
            interval_mutator=AsyncMock(return_value=uuid4()),
            reconcile_intervals=False,
        )


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

    bounded = ShadowComparison(
        source_uid="pod-1",
        status=ShadowComparisonStatus.LIFETIME_MISMATCH,
        reason_code="bounded-start-semantics",
        explained=True,
        comparison_at=NOW,
        owner_trusted=True,
        owner_kind="job",
        owner_id=uuid4(),
        legacy_interval_id=8,
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
    assert bounded.explained is True

    with pytest.raises(InventoryContractError, match="bounded start evidence"):
        ShadowComparison(
            source_uid="pod-1",
            status=ShadowComparisonStatus.LIFETIME_MISMATCH,
            reason_code="bounded-start-semantics",
            explained=True,
            comparison_at=NOW,
            owner_trusted=True,
            owner_kind="job",
            owner_id=uuid4(),
            legacy_interval_id=9,
            legacy_cpu_millicores=500,
            legacy_memory_bytes=1024,
            legacy_started_at=legacy_started_at,
            observed_cpu_millicores=500,
            observed_memory_bytes=1024,
            observed_started_at=NOW,
            observed_start_time_source="app-db-received",
            observed_start_uncertainty_us=4_999_999,
            start_delta_us=5_000_000,
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
