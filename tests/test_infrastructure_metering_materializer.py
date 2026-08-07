"""Deterministic segmentation and strict cross-database publication tests."""

from __future__ import annotations

import copy
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from orchestrator.services.infrastructure_metering import (
    materializer as materializer_sql,
)
from orchestrator.services.infrastructure_metering.materializer import (
    CorrectionDelta,
    CorrectionRequestDelta,
    FrozenPublicationPlan,
    InfrastructureUsageMaterializer,
    PublicationConflictError,
    PublicationContractError,
    PublicationDisabledError,
    PublicationFenceError,
    StoragePublicationAuthority,
    StoragePublicationPolicy,
    build_correction_plan,
    build_late_usage_plan,
    build_usage_plan,
)
from orchestrator.services.usage_ledger import (
    StrictUsageConflict,
    StrictUsageExpectation,
    StrictUsagePartitionMissing,
    UsageLedger,
    UsageRates,
)


UTC = timezone.utc
START = datetime(2026, 8, 5, 23, 30, tzinfo=UTC)
INTERVAL_ID = UUID("10000000-0000-0000-0000-000000000001")
LIFECYCLE_ID = UUID("20000000-0000-0000-0000-000000000002")
OWNER_ID = UUID("30000000-0000-0000-0000-000000000003")
USER_ID = UUID("40000000-0000-0000-0000-000000000004")
PROJECT_ID = UUID("50000000-0000-0000-0000-000000000005")
PLAN_ID = UUID("60000000-0000-0000-0000-000000000006")
LOCAL_STORAGE_SCOPE_ID = UUID("70000000-0000-0000-0000-000000000007")
REMOTE_STORAGE_SCOPE_ID = UUID("80000000-0000-0000-0000-000000000008")
COMPUTE_SCOPE_ID = UUID("90000000-0000-0000-0000-000000000009")
COMPUTE_EPOCH_ID = UUID("a0000000-0000-0000-0000-00000000000a")


def _interval(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": INTERVAL_ID,
        "source_cluster": "main-dev",
        "source_kind": "pod",
        "source_uid": "pod-uid-1",
        "source_lifecycle_id": LIFECYCLE_ID,
        "revision_no": 1,
        "source_revision": "a" * 64,
        "namespace": "srw",
        "name": "workspace-pod-1",
        "category": "compute",
        "resource": "workspace_pod",
        "measurement_basis": "scheduler-request",
        "cost_domain": "workload-allocation",
        "resource_class": "kubernetes-pod",
        "attribution_scope": "customer",
        "owner_kind": "job",
        "owner_id": str(OWNER_ID),
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
        "attribution_source": "job-label-db",
        "attribution_quality": "exact",
        "backing_resource_uid": None,
        "lifecycle_confidence": "kubernetes-visible",
        "cpu_millicores": 8000,
        "memory_bytes": 16 * 1024**3,
        "storage_bytes": None,
        "capacity_source": "pod-requests-v1",
        "capacity_quality": "exact",
        "measurement_algorithm": "kubernetes-pod-requests-v1",
        "started_at": START,
        "start_time_source": "kubernetes-creation",
        "start_uncertainty_us": 0,
        "ended_at": START + timedelta(hours=1, minutes=30),
        "end_time_source": "complete-list-absence",
        "end_uncertainty_us": 1_000_000,
        "last_seen_at": START + timedelta(hours=1),
        "last_confirmed_at": START + timedelta(hours=1, minutes=30),
        "materialized_through": START,
        "end_reason": "absent-complete-list",
    }
    row.update(overrides)
    return row


def _rate(
    rate_id: str,
    unit: str,
    price: str,
    effective_from: datetime,
    effective_to: datetime | None = None,
) -> dict[str, Any]:
    return {
        "id": UUID(rate_id),
        "unit": unit,
        "usd_per_unit": Decimal(price),
        "effective_from": effective_from,
        "effective_to": effective_to,
    }


def _plan(**kwargs: Any):
    plan = build_usage_plan(
        _interval(**kwargs), (), creator_generation=7, plan_id=PLAN_ID
    )
    assert plan is not None
    return plan


def _compute_interval(activation_key: str) -> dict[str, Any]:
    if activation_key == "agent_pod":
        return _interval(
            inventory_scope_id=COMPUTE_SCOPE_ID,
            compute_scope_epoch_id=COMPUTE_EPOCH_ID,
            resource="agent_pod",
            details={"product_class": "dynamic-agent"},
        )
    if activation_key == "ide_workspace_pod":
        return _interval(
            inventory_scope_id=COMPUTE_SCOPE_ID,
            compute_scope_epoch_id=COMPUTE_EPOCH_ID,
            details={"product_class": "ide-session"},
        )
    if activation_key == "workspace_vm":
        return _interval(
            inventory_scope_id=COMPUTE_SCOPE_ID,
            compute_scope_epoch_id=COMPUTE_EPOCH_ID,
            source_kind="vmi",
            resource="workspace_vm",
            measurement_basis="guest-provisioned",
            resource_class="virtual-machine",
            capacity_source="vmi-guest-provisioned",
            details={"product_class": "workspace-vm"},
        )
    raise AssertionError(activation_key)


def _compute_activation_row(
    activation_key: str,
    *,
    state: str = "active",
    activated_at: datetime | None = None,
    database_time: datetime | None = None,
) -> dict[str, Any]:
    return {
        "activation_key": activation_key,
        "state": state,
        "activated_at": (
            START - timedelta(days=1)
            if state == "active" and activated_at is None
            else activated_at
        ),
        "database_time": (
            START + timedelta(days=1)
            if state == "active" and database_time is None
            else database_time
        ),
    }


def _compute_exact_epoch_row() -> dict[str, Any]:
    return {
        "effective_from": START - timedelta(days=1),
        "inventory_scope_epoch_id": COMPUTE_EPOCH_ID,
        "retired_at": None,
    }


def _storage_interval(
    *,
    remote: bool = False,
    measurement_basis: str = "claim-requested",
    resource: str | None = None,
) -> dict[str, Any]:
    is_claim = measurement_basis == "claim-requested"
    selected_resource = resource
    if selected_resource is None:
        if is_claim:
            selected_resource = "vm_rootdisk_claim" if remote else "workspace_pvc"
        else:
            selected_resource = "unmapped_block_volume"
    return _interval(
        inventory_scope_id=(
            REMOTE_STORAGE_SCOPE_ID if remote else LOCAL_STORAGE_SCOPE_ID
        ),
        source_cluster="vm-dev" if remote else "main-dev",
        source_kind="pvc" if is_claim else "volume",
        source_uid="pvc-uid-1" if is_claim else "volume-digest-1",
        category="storage",
        resource=selected_resource,
        measurement_basis=measurement_basis,
        cost_domain="workload-allocation" if is_claim else "physical-asset",
        resource_class=("persistent-volume-claim" if is_claim else "persistent-volume"),
        cpu_millicores=None,
        memory_bytes=None,
        storage_bytes=20 * 1024**3,
        capacity_source=(
            "pvc-requested-storage" if is_claim else "pv-provisioned-capacity"
        ),
        measurement_algorithm=(
            "kubernetes-pvc-request-v1" if is_claim else "kubernetes-pv-capacity-v1"
        ),
    )


def _storage_authority(
    *,
    remote: bool = False,
    measurement_basis: str = "claim-requested",
) -> StoragePublicationAuthority:
    return StoragePublicationAuthority(
        measurement_basis=measurement_basis,
        collector_id="kubevirt-storage" if remote else "kubernetes-pods",
        source_cluster="vm-dev" if remote else "main-dev",
    )


def _storage_source_fence(
    *,
    remote: bool = False,
    measurement_basis: str = "claim-requested",
    requirement_role: str = "quantity",
    source_state: str = "active",
    global_state: str = "active",
    source_activated_at: datetime | None = None,
    global_activated_at: datetime | None = None,
    database_time: datetime | None = None,
) -> dict[str, Any]:
    return {
        "collector_id": "kubevirt-storage" if remote else "kubernetes-pods",
        "source_cluster": "vm-dev" if remote else "main-dev",
        "requirement_role": requirement_role,
        "source_state": source_state,
        "source_activated_at": (
            START - timedelta(days=1)
            if source_state == "active" and source_activated_at is None
            else source_activated_at
        ),
        "global_state": global_state,
        "global_activated_at": (
            START - timedelta(days=2)
            if global_state == "active" and global_activated_at is None
            else global_activated_at
        ),
        "database_time": database_time or START + timedelta(days=1),
        "measurement_basis": measurement_basis,
    }


def test_compute_plan_splits_at_utc_midnight_and_multiplies_capacity() -> None:
    plan = _plan()

    assert plan.period_start == START
    assert plan.period_end == datetime(2026, 8, 6, tzinfo=UTC)
    rows = {item.event.payload["unit"]: item.event.payload for item in plan.events}
    assert rows["vcpu-hour"]["quantity"] == "4"
    assert rows["gib-hour"]["quantity"] == "8"
    assert rows["vcpu-hour"]["source_capacity_value"] == "8000"
    assert rows["gib-hour"]["source_capacity_value"] == str(16 * 1024**3)
    assert rows["vcpu-hour"]["rate_usd"] is None
    assert rows["vcpu-hour"]["cost_usd"] is None
    assert rows["vcpu-hour"]["period_start"].endswith(".000000Z")


def test_plan_splits_on_any_unit_rate_boundary_and_snapshots_free_rate() -> None:
    boundary = START + timedelta(minutes=15)
    rates = (
        _rate(
            "70000000-0000-0000-0000-000000000007",
            "vcpu-hour",
            "0.10",
            START - timedelta(days=1),
            boundary,
        ),
        _rate(
            "71000000-0000-0000-0000-000000000007",
            "vcpu-hour",
            "0.20",
            boundary,
        ),
        _rate(
            "72000000-0000-0000-0000-000000000007",
            "gib-hour",
            "0",
            START - timedelta(days=1),
        ),
    )

    plan = build_usage_plan(_interval(), rates, creator_generation=7, plan_id=PLAN_ID)
    assert plan is not None
    assert plan.period_end == boundary
    rows = {item.event.payload["unit"]: item.event.payload for item in plan.events}
    assert rows["vcpu-hour"]["quantity"] == "2"
    assert rows["vcpu-hour"]["rate_usd"] == "0.1"
    assert rows["vcpu-hour"]["cost_usd"] == "0.2"
    assert rows["gib-hour"]["quantity"] == "4"
    assert rows["gib-hour"]["rate_usd"] == "0"
    assert rows["gib-hour"]["cost_usd"] == "0"


def test_unmapped_physical_volume_is_forcibly_unpriced() -> None:
    rates = (
        _rate(
            "73000000-0000-0000-0000-000000000007",
            "gib-hour",
            "999",
            START - timedelta(days=1),
        ),
        _rate(
            "74000000-0000-0000-0000-000000000007",
            "volume-hour",
            "999",
            START - timedelta(days=1),
        ),
    )
    plan = build_usage_plan(
        _interval(
            source_kind="volume",
            category="storage",
            resource="unmapped_block_volume",
            measurement_basis="volume-provisioned",
            cost_domain="physical-asset",
            resource_class="persistent-volume",
            cpu_millicores=None,
            memory_bytes=None,
            storage_bytes=4 * 1024**3,
        ),
        rates,
        creator_generation=7,
        plan_id=PLAN_ID,
    )

    assert plan is not None
    assert {event.event.payload["unit"] for event in plan.events} == {
        "gib-hour",
        "volume-hour",
    }
    for event in plan.events:
        assert event.canonical_rate_version_id is None
        assert event.event.payload["rate_usd"] is None
        assert event.event.payload["cost_usd"] is None


def test_mapped_physical_volume_requires_and_exports_rule_provenance() -> None:
    interval = _interval(
        source_kind="volume",
        category="storage",
        resource="block_volume_local_path",
        measurement_basis="volume-provisioned",
        cost_domain="physical-asset",
        resource_class="persistent-volume",
        cpu_millicores=None,
        memory_bytes=None,
        storage_bytes=4 * 1024**3,
        details={
            "mapping_version": "local-v1",
            "mapping_fingerprint": "b" * 64,
            "storage_asset_id": str(INTERVAL_ID),
        },
    )
    plan = build_usage_plan(interval, (), creator_generation=7, plan_id=PLAN_ID)

    assert plan is not None
    for event in plan.events:
        assert event.event.payload["details"]["mapping_version"] == "local-v1"
        assert event.event.payload["details"]["mapping_fingerprint"] == "b" * 64

    with pytest.raises(PublicationContractError, match="mapping provenance"):
        build_usage_plan(
            {**interval, "details": {}},
            (),
            creator_generation=7,
            plan_id=PLAN_ID,
        )


def test_open_interval_publishes_only_complete_confirmed_utc_days() -> None:
    open_row = _interval(
        started_at=datetime(2026, 8, 5, tzinfo=UTC),
        materialized_through=datetime(2026, 8, 5, tzinfo=UTC),
        ended_at=None,
        end_time_source=None,
        end_uncertainty_us=None,
        end_reason=None,
        last_confirmed_at=datetime(2026, 8, 6, 12, tzinfo=UTC),
    )
    plan = build_usage_plan(open_row, (), creator_generation=7)
    assert plan is not None
    assert plan.period_end == datetime(2026, 8, 6, tzinfo=UTC)

    open_row["materialized_through"] = datetime(2026, 8, 6, tzinfo=UTC)
    assert build_usage_plan(open_row, (), creator_generation=7) is None


def test_pvc_records_capacity_and_occurrence_hours_separately() -> None:
    plan = build_usage_plan(
        _interval(
            source_kind="pvc",
            category="storage",
            resource="workspace_claim",
            measurement_basis="claim-requested",
            resource_class="persistent-volume-claim",
            cpu_millicores=None,
            memory_bytes=None,
            storage_bytes=4 * 1024**3,
            materialized_through=datetime(2026, 8, 5, 10, tzinfo=UTC),
            started_at=datetime(2026, 8, 5, 10, tzinfo=UTC),
            ended_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
            last_seen_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
            last_confirmed_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
        ),
        (),
        creator_generation=7,
    )
    assert plan is not None
    rows = {item.event.payload["unit"]: item.event.payload for item in plan.events}
    assert rows["gib-hour"]["quantity"] == "8"
    assert rows["claim-hour"]["quantity"] == "2"
    assert rows["claim-hour"]["source_capacity_value"] == "1"


def test_plan_hashes_do_not_depend_on_random_plan_identity() -> None:
    first = _plan()
    second = build_usage_plan(
        _interval(),
        (),
        creator_generation=7,
        plan_id=UUID("61000000-0000-0000-0000-000000000006"),
    )
    assert second is not None
    assert first.event_set_hash == second.event_set_hash
    assert first.rate_selection_hash == second.rate_selection_hash
    assert [item.event.row_hash for item in first.events] == [
        item.event.row_hash for item in second.events
    ]


def test_late_usage_plan_freezes_discovery_evidence_and_advances_cursor() -> None:
    discovered_at = START + timedelta(days=2)
    plan = build_late_usage_plan(
        _interval(),
        (),
        creator_generation=7,
        discovered_at=discovered_at,
        discovery_evidence={
            "kind": "complete-list-backfill",
            "snapshot_id": "snapshot-1",
        },
        plan_id=UUID("83000000-0000-0000-0000-000000000008"),
    )

    assert plan is not None
    assert plan.plan_kind == "late-usage"
    assert plan.plan_revision == 0
    assert plan.advances_cursor
    assert plan.previous_materialized_through == plan.period_start
    assert plan.correction_group_id is None
    assert all(
        item.event.payload["event_kind"] == "late-usage"
        and item.event.payload["discovered_at"]
        == discovered_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
        and item.event.payload["details"]["discovery_evidence"]["snapshot_id"]
        == "snapshot-1"
        for item in plan.events
    )


def test_late_usage_plan_requires_post_period_durable_evidence() -> None:
    with pytest.raises(PublicationContractError, match="durable discovery evidence"):
        build_late_usage_plan(
            _interval(),
            (),
            creator_generation=7,
            discovered_at=START + timedelta(days=1),
            discovery_evidence={},
        )

    with pytest.raises(PublicationContractError, match="before period end"):
        build_late_usage_plan(
            _interval(),
            (),
            creator_generation=7,
            discovered_at=START + timedelta(minutes=1),
            discovery_evidence={"kind": "backfill"},
        )


def test_correction_plan_atomically_reverses_and_reattributes_original() -> None:
    original = _plan().events[0]
    replacement_user = UUID("81000000-0000-0000-0000-000000000008")
    replacement_project = UUID("82000000-0000-0000-0000-000000000008")
    correction = build_correction_plan(
        _interval(),
        (
            CorrectionDelta(original=original, quantity="-4"),
            CorrectionDelta(
                original=original,
                quantity="4",
                payload_overrides={
                    "user_id": replacement_user,
                    "project_id": replacement_project,
                    "ref_id": OWNER_ID,
                    "details": {"review_ticket": "BILL-42"},
                },
            ),
        ),
        correction_reason="reviewed attribution repair",
        correction_actor_id=USER_ID,
        creator_generation=7,
        plan_revision=1,
        discovered_at=START + timedelta(days=1),
        plan_id=UUID("84000000-0000-0000-0000-000000000008"),
    )

    assert correction.plan_kind == "correction"
    assert correction.plan_revision == 1
    assert not correction.advances_cursor
    assert correction.previous_materialized_through is None
    assert correction.correction_group_id == correction.id
    assert [item.event.payload["quantity"] for item in correction.events] == [
        "-4",
        "4",
    ]
    assert correction.events[1].event.payload["user_id"] == str(replacement_user)
    assert correction.events[1].event.payload["project_id"] == str(replacement_project)
    assert all(
        item.event.payload["corrects_source_id"] == original.event.payload["source_id"]
        and item.event.payload["corrects_unit"] == "vcpu-hour"
        and item.event.payload["correction_group_id"] == str(correction.id)
        and item.event.payload["details"]["corrects_payload_hash"]
        == original.event.row_hash
        for item in correction.events
    )
    assert len({item.event.payload["source_id"] for item in correction.events}) == 2


def test_correction_plan_rejects_reversal_larger_than_original_integral() -> None:
    original = _plan().events[0]
    with pytest.raises(
        PublicationContractError, match="exceeds original capacity-time"
    ):
        build_correction_plan(
            _interval(),
            (CorrectionDelta(original=original, quantity="-5"),),
            correction_reason="excess reversal",
            correction_actor_id=USER_ID,
            creator_generation=7,
            plan_revision=1,
        )


def test_correction_plan_rejects_final_payload_that_audit_cannot_accept() -> None:
    original = _plan().events[0]

    with pytest.raises(PublicationContractError, match="non-customer correction"):
        build_correction_plan(
            _interval(),
            (
                CorrectionDelta(
                    original=original,
                    quantity="4",
                    payload_overrides={"attribution_scope": "unknown"},
                ),
            ),
            correction_reason="invalid attribution shape",
            correction_actor_id=USER_ID,
            creator_generation=7,
            plan_revision=1,
        )

    with pytest.raises(PublicationContractError, match="typed audit resource"):
        build_correction_plan(
            _interval(),
            (
                CorrectionDelta(
                    original=original,
                    quantity="4",
                    payload_overrides={"source_capacity_unit": "byte"},
                ),
            ),
            correction_reason="invalid capacity shape",
            correction_actor_id=USER_ID,
            creator_generation=7,
            plan_revision=1,
        )


def test_negative_correction_cannot_move_dimensions_or_replace_rate() -> None:
    original = _plan().events[0]

    with pytest.raises(PublicationContractError, match="changed original dimensions"):
        build_correction_plan(
            _interval(),
            (
                CorrectionDelta(
                    original=original,
                    quantity="-4",
                    payload_overrides={
                        "user_id": UUID("83000000-0000-0000-0000-000000000008")
                    },
                ),
            ),
            correction_reason="invalid reversal attribution",
            correction_actor_id=USER_ID,
            creator_generation=7,
            plan_revision=1,
        )

    with pytest.raises(
        PublicationContractError, match="must inherit the original rate"
    ):
        build_correction_plan(
            _interval(),
            (
                CorrectionDelta(
                    original=original,
                    quantity="-4",
                    payload_overrides={"rate_usd": "0.25"},
                    inherit_rate=False,
                    canonical_rate_version_id=UUID(
                        "84000000-0000-0000-0000-000000000008"
                    ),
                ),
            ),
            correction_reason="invalid reversal price",
            correction_actor_id=USER_ID,
            creator_generation=7,
            plan_revision=1,
        )

    with pytest.raises(PublicationContractError, match="changed original dimensions"):
        build_correction_plan(
            _interval(),
            (
                CorrectionDelta(
                    original=original,
                    quantity="-1",
                    payload_overrides={"source_capacity_value": "2000"},
                ),
            ),
            correction_reason="invalid reversal capacity provenance",
            correction_actor_id=USER_ID,
            creator_generation=7,
            plan_revision=1,
        )


def test_inherited_correction_rate_cannot_change_canonical_selectors() -> None:
    rate_id = "85000000-0000-0000-0000-000000000008"
    priced = build_usage_plan(
        _interval(),
        (_rate(rate_id, "vcpu-hour", "0.25", START - timedelta(days=1)),),
        creator_generation=7,
    )
    assert priced is not None
    original = next(
        item for item in priced.events if item.event.payload["unit"] == "vcpu-hour"
    )

    with pytest.raises(PublicationContractError, match="rate selector changed"):
        build_correction_plan(
            _interval(),
            (
                CorrectionDelta(
                    original=original,
                    quantity="4",
                    payload_overrides={"resource": "different_resource"},
                ),
            ),
            correction_reason="invalid inherited selector",
            correction_actor_id=USER_ID,
            creator_generation=7,
            plan_revision=1,
        )


def test_loaded_plan_recomputes_payload_hash_instead_of_trusting_jsonb() -> None:
    plan = _plan()
    rows = _event_rows(plan)
    rows[0]["event_payload"]["quantity"] = "999"

    with pytest.raises(PublicationContractError, match="payload hash mismatch"):
        FrozenPublicationPlan.from_records(_plan_row(plan), rows)


def test_pending_plan_priority_rotates_failed_attempts_behind_fresh_intent() -> None:
    sql = materializer_sql._PENDING_PLAN_SQL

    assert "last_attempt_at ASC NULLS FIRST" in sql
    assert "attempt_count, created_at, id" in sql


class _Acquire:
    def __init__(self, connection: Any):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args: Any):
        return False


class _AuditTransaction:
    def __init__(self, connection: _AuditConnection):
        self.connection = connection
        self.before: Any = None

    async def __aenter__(self):
        self.before = copy.deepcopy(
            (self.connection.pool.rows, self.connection.pool.payloads)
        )
        return self

    async def __aexit__(self, exc_type: Any, *_args: Any):
        if exc_type is not None:
            self.connection.pool.rows, self.connection.pool.payloads = self.before
        return False


class _AuditConnection:
    def __init__(self, pool: _AuditPool):
        self.pool = pool

    def transaction(self, **_kwargs: Any):
        return _AuditTransaction(self)

    async def fetch(self, sql: str, *args: Any):
        if "strict-usage:attached-partitions" in sql:
            return [{"relname": name} for name in args[0] if name in self.pool.attached]
        if "strict-usage:verify-frozen" in sql:
            expected = json.loads(args[0])
            return [
                {
                    "source": item["source"],
                    "source_id": item["source_id"],
                    "unit": item["unit"],
                    "ts": item["ts"],
                    "expected_hash": item["payload_hash"],
                    "actual_hash": self.pool.rows.get(
                        (
                            item["source"],
                            item["source_id"],
                            item["unit"],
                            item["ts"],
                        )
                    ),
                }
                for item in expected
            ]
        if "strict-usage:verify-expected" in sql:
            expected = json.loads(args[0])
            return [
                {
                    **item,
                    "present": (
                        item["source"],
                        item["source_id"],
                        item["unit"],
                        item["ts"],
                    )
                    in self.pool.payloads,
                    "actual_payload": self.pool.payloads.get(
                        (
                            item["source"],
                            item["source_id"],
                            item["unit"],
                            item["ts"],
                        )
                    ),
                }
                for item in expected
            ]
        raise AssertionError(f"unexpected audit fetch: {sql}")

    async def execute(self, sql: str, *args: Any):
        if "strict-usage:insert-expected" in sql:
            self.pool.insert_calls += 1
            inserted = 0
            for payload in json.loads(args[0]):
                key = (
                    payload["source"],
                    payload["source_id"],
                    payload["unit"],
                    payload["ts"],
                )
                if key not in self.pool.rows:
                    self.pool.rows[key] = ""
                    self.pool.payloads[key] = copy.deepcopy(payload)
                    inserted += 1
            return f"INSERT 0 {inserted}"
        if "strict-usage:insert-frozen" not in sql:
            raise AssertionError(f"unexpected audit execute: {sql}")
        self.pool.insert_calls += 1
        inserted = 0
        for payload in json.loads(args[0]):
            key = (
                payload["source"],
                payload["source_id"],
                payload["unit"],
                payload["ts"],
            )
            if key not in self.pool.rows:
                self.pool.rows[key] = payload["payload_hash"]
                self.pool.payloads[key] = copy.deepcopy(payload)
                inserted += 1
        return f"INSERT 0 {inserted}"


class _AuditPool:
    def __init__(self, *, attached: set[str] | None = None):
        self.attached = attached or {"usage_events_p2026_08"}
        self.rows: dict[tuple[str, str, str, str], str] = {}
        self.payloads: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.insert_calls = 0

    def acquire(self):
        return _Acquire(_AuditConnection(self))


@pytest.mark.asyncio
async def test_strict_ledger_inserts_then_verifies_exact_replay() -> None:
    plan = _plan()
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    events = [item.event for item in plan.events]

    first = await ledger.publish_frozen_events(events)
    replay = await ledger.publish_frozen_events(events)

    assert (first.expected, first.inserted, first.verified) == (2, 2, 2)
    assert (replay.expected, replay.inserted, replay.verified) == (2, 0, 2)
    assert audit.insert_calls == 2
    assert len(audit.rows) == 2


@pytest.mark.asyncio
async def test_strict_ledger_rolls_back_batch_on_hash_conflict() -> None:
    plan = _plan()
    audit = _AuditPool()
    first = plan.events[0].event
    audit.rows[first.dedupe_key] = "f" * 64
    before = copy.deepcopy(audit.rows)
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]

    with pytest.raises(StrictUsageConflict, match="hash mismatch"):
        await ledger.publish_frozen_events([item.event for item in plan.events])

    assert audit.rows == before


@pytest.mark.asyncio
async def test_strict_ledger_refuses_missing_partition_before_insert() -> None:
    plan = _plan()
    audit = _AuditPool(attached={"usage_events_p2026_07"})
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]

    with pytest.raises(StrictUsagePartitionMissing) as raised:
        await ledger.publish_frozen_events([item.event for item in plan.events])

    assert raised.value.partitions == ("usage_events_p2026_08",)
    assert audit.insert_calls == 0


@pytest.mark.asyncio
async def test_strict_ledger_verifies_frozen_original_without_mutation() -> None:
    plan = _plan()
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    events = [item.event for item in plan.events]
    await ledger.publish_frozen_events(events)
    before = copy.deepcopy((audit.rows, audit.payloads, audit.insert_calls))

    result = await ledger.verify_frozen_events(events)

    assert (result.expected, result.inserted, result.verified) == (2, 0, 2)
    assert (audit.rows, audit.payloads, audit.insert_calls) == before


@pytest.mark.asyncio
async def test_strict_ledger_legacy_expectation_detects_immutable_field_mismatch() -> (
    None
):
    plan = _plan()
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    event = plan.events[0].event
    await ledger.publish_frozen_events([event])
    expectation = StrictUsageExpectation(
        source=event.payload["source"],
        source_id=event.payload["source_id"],
        unit=event.payload["unit"],
        ts=event.timestamp,
        expected_fields={
            "category": "compute",
            "resource": "workspace_pod",
            "quantity": "999",
            "user_id": str(USER_ID),
        },
    )

    with pytest.raises(StrictUsageConflict, match="quantity"):
        await ledger.verify_expected_events([expectation])

    assert audit.insert_calls == 1


def _legacy_expectation(*, quantity: str = "4") -> StrictUsageExpectation:
    fields = {
        "ts": START,
        "user_id": USER_ID,
        "project_id": PROJECT_ID,
        "ref_kind": "job",
        "ref_id": OWNER_ID,
        "category": "compute",
        "resource": "workspace_pod",
        "quantity": quantity,
        "unit": "vcpu-hour",
        "rate_usd": "0.1",
        "cost_usd": str(Decimal(quantity) * Decimal("0.1")),
        "source": "orchestrator",
        "source_id": "ws:job:legacy:1",
        "details": {"tier": "container", "frozen": True},
    }
    return StrictUsageExpectation(
        source="orchestrator",
        source_id="ws:job:legacy:1",
        unit="vcpu-hour",
        ts=START,
        expected_fields=fields,
    )


@pytest.mark.asyncio
async def test_strict_legacy_repair_inserts_and_verifies_without_rate_lookup() -> None:
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]

    first = await ledger.publish_expected_events([_legacy_expectation()])
    replay = await ledger.publish_expected_events([_legacy_expectation()])

    assert (first.expected, first.inserted, first.verified) == (1, 1, 1)
    assert (replay.expected, replay.inserted, replay.verified) == (1, 0, 1)
    before = copy.deepcopy((audit.rows, audit.payloads))
    with pytest.raises(StrictUsageConflict, match="quantity"):
        await ledger.publish_expected_events([_legacy_expectation(quantity="5")])
    assert (audit.rows, audit.payloads) == before


class _PlanningConnection:
    def __init__(self, pool: _PlanningPool):
        self.pool = pool

    def transaction(self, **_kwargs: Any):
        return _AppTransaction(self)  # type: ignore[arg-type]

    async def fetchrow(self, sql: str, *args: Any):
        if "infra-publication:control" in sql:
            return {
                "leader_generation": self.pool.generation,
                "cutover_state": self.pool.cutover_state,
                "cutover_at": START,
            }
        if "infra-publication:day-state" in sql:
            return {
                "state": self.pool.day_state,
                "coverage_status": self.pool.coverage_status,
                "coverage_revision": self.pool.coverage_revision,
                "coverage_sequence": self.pool.coverage_sequence,
                "unknown_ranges": copy.deepcopy(self.pool.unknown_ranges),
            }
        if "infra-publication:degrade-sealed-day" in sql:
            _, old_sequence, old_revision, new_revision, raw_unknown = args
            if (
                self.pool.day_state != "sealed"
                or self.pool.coverage_sequence != old_sequence
                or self.pool.coverage_revision != old_revision
            ):
                return None
            self.pool.coverage_status = "partial"
            self.pool.coverage_sequence += 1
            self.pool.coverage_revision = new_revision
            self.pool.unknown_ranges.extend(json.loads(raw_unknown))
            return {
                "coverage_sequence": self.pool.coverage_sequence,
                "coverage_revision": new_revision,
            }
        if "infra-publication:lock-interval" in sql:
            return copy.deepcopy(self.pool.interval)
        if "infra-publication:compute-activation-fence" in sql:
            return copy.deepcopy(self.pool.compute_activations.get(str(args[0])))
        if "infra-publication:storage-source-fence" in sql:
            return copy.deepcopy(
                self.pool.storage_source_fences.get((args[0], str(args[1])))
            )
        if "infra-publication:correction-original" in sql:
            source, source_id, unit, ts, row_hash = args
            for event in self.pool.original_plan.events:
                payload = event.event.payload
                if (
                    (source, source_id, unit)
                    == (payload["source"], payload["source_id"], payload["unit"])
                    and ts == event.event.timestamp
                    and row_hash == event.event.row_hash
                ):
                    return {
                        "original_plan_id": self.pool.original_plan.id,
                        "source_interval_id": self.pool.original_plan.source_interval_id,
                        "source_revision": self.pool.original_plan.source_revision,
                        "plan_kind": self.pool.original_plan.plan_kind,
                        "state": "published",
                        "ordinal": event.ordinal,
                        "canonical_rate_version_id": (event.canonical_rate_version_id),
                        "row_hash": event.event.row_hash,
                        "event_payload": copy.deepcopy(payload),
                    }
            return None
        if "infra-publication:correction-interval" in sql:
            return copy.deepcopy(self.pool.interval)
        if "infra-publication:correction-plan-by-id" in sql:
            if self.pool.inserted_plan is None or self.pool.inserted_plan[0] != args[0]:
                return None
            plan = self.pool.inserted_plan
            return {
                "id": plan[0],
                "source_interval_id": plan[1],
                "source_revision": plan[2],
                "plan_kind": plan[3],
                "plan_revision": plan[4],
                "advances_cursor": plan[5],
                "previous_materialized_through": plan[6],
                "correction_group_id": plan[7],
                "period_start": plan[8],
                "period_end": plan[9],
                "expected_event_count": plan[10],
                "payload_schema_version": plan[11],
                "event_set_hash": plan[12],
                "rate_selection_hash": plan[13],
                "creator_generation": plan[14],
                "state": "planned",
            }
        raise AssertionError(f"unexpected planning fetchrow: {sql}")

    async def fetchval(self, sql: str, *args: Any):
        if "infra-publication:next-correction-revision" in sql:
            return self.pool.next_correction_revision
        if "infra-publication:correction-rate" in sql:
            for row in (*self.pool.replacement_rates, *self.pool.rates):
                selectors = {
                    "cost_domain": self.pool.interval["cost_domain"],
                    "measurement_basis": self.pool.interval["measurement_basis"],
                    "category": self.pool.interval["category"],
                    "resource_class": self.pool.interval["resource_class"],
                    "resource": self.pool.interval["resource"],
                    **row,
                }
                if (
                    args[:8]
                    == (
                        selectors["id"],
                        selectors["unit"],
                        Decimal(str(selectors["usd_per_unit"])),
                        selectors["cost_domain"],
                        selectors["measurement_basis"],
                        selectors["category"],
                        selectors["resource_class"],
                        selectors["resource"],
                    )
                    and selectors["effective_from"] <= args[8]
                    and (
                        selectors.get("effective_to") is None
                        or selectors["effective_to"] >= args[9]
                    )
                ):
                    self.pool.locked_rate_ids.append(selectors["id"])
                    return True
            return None
        raise AssertionError(f"unexpected planning fetchval: {sql}")

    async def fetch(self, sql: str, *args: Any):
        if "infra-publication:candidates" in sql:
            self.pool.candidate_args = args
            return [copy.deepcopy(self.pool.interval)]
        if "infra-publication:rates" in sql:
            self.pool.rate_args = args
            return copy.deepcopy(self.pool.rates)
        if "infra-publication:plan-events" in sql:
            return copy.deepcopy(self.pool.inserted_events)
        if "infra-publication:lock-correction-originals" in sql:
            keys = list(zip(args[0], args[1], args[2], args[3]))
            rows = []
            for key in keys:
                original = next(
                    (
                        item
                        for item in self.pool.original_plan.events
                        if item.event.dedupe_key == key
                    ),
                    None,
                )
                if original is not None:
                    self.pool.locked_original_keys.append(key)
                    rows.append(
                        {
                            "source": key[0],
                            "source_id": key[1],
                            "unit": key[2],
                            "corrects_ts": key[3],
                            "original_quantity": original.event.payload["quantity"],
                            "original_row_hash": original.event.row_hash,
                        }
                    )
            return rows
        if "infra-publication:correction-negative-totals" in sql:
            keys = list(zip(args[0], args[1], args[2], args[3]))
            return [
                {
                    "ordinality": ordinal,
                    "negative_quantity": self.pool.prior_negative_quantities.get(
                        key, Decimal(0)
                    ),
                }
                for ordinal, key in enumerate(keys, start=1)
            ]
        raise AssertionError(f"unexpected planning fetch: {sql}")

    async def execute(self, sql: str, *args: Any):
        if "infra-publication:ensure-day-state" in sql:
            self.pool.ensured_days.append(args[0])
            return "INSERT 0 0"
        if "infra-publication:insert-plan-events" in sql:
            self.pool.inserted_events = json.loads(args[0])
            return f"INSERT 0 {len(self.pool.inserted_events)}"
        if "infra-publication:insert-plan" in sql:
            self.pool.inserted_plan = args
            self.pool.plan_insert_calls += 1
            return "INSERT 0 1"
        raise AssertionError(f"unexpected planning execute: {sql}")


class _PlanningPool:
    def __init__(self):
        self.interval = _interval()
        self.original_plan = _plan()
        self.rates: list[dict[str, Any]] = []
        self.replacement_rates: list[dict[str, Any]] = []
        self.locked_rate_ids: list[UUID] = []
        self.locked_original_keys: list[tuple[str, str, str, str]] = []
        self.prior_negative_quantities: dict[tuple[str, str, str, str], Decimal] = {}
        self.generation = 7
        self.cutover_state = "active"
        self.day_state = "open"
        self.coverage_status: str | None = None
        self.coverage_revision: str | None = None
        self.coverage_sequence = 0
        self.unknown_ranges: list[dict[str, Any]] = []
        self.next_correction_revision = 1
        self.compute_activations: dict[str, dict[str, Any]] = {}
        self.storage_source_fences: dict[tuple[UUID, str], dict[str, Any]] = {}
        self.candidate_args: tuple[Any, ...] | None = None
        self.rate_args: tuple[Any, ...] | None = None
        self.inserted_plan: tuple[Any, ...] | None = None
        self.plan_insert_calls = 0
        self.inserted_events: list[dict[str, Any]] = []
        self.ensured_days: list[date] = []
        # _AppTransaction snapshots these two attributes. Planning does not
        # mutate them, but sharing the transaction fake keeps rollback behavior
        # explicit in this unit boundary.
        self.plan = None
        self.cursor = START

    def acquire(self):
        return _Acquire(_PlanningConnection(self))


@pytest.mark.asyncio
async def test_manual_freeze_cannot_bypass_ide_subtype_gate() -> None:
    interval = _compute_interval("ide_workspace_pod")
    plan = build_usage_plan(interval, (), creator_generation=7)
    assert plan is not None
    app = _PlanningPool()
    app.interval = interval
    app.compute_activations["ide_workspace_pod"] = _compute_activation_row(
        "ide_workspace_pod"
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        ide_workspace_pod_enabled=False,
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationDisabledError, match="IDE workspace Pod"):
        await materializer.freeze_plan(plan, 7)

    assert app.inserted_plan is None


@pytest.mark.asyncio
@pytest.mark.parametrize("activation_key", ["agent_pod", "workspace_vm"])
async def test_manual_freeze_cannot_bypass_compute_class_gate(
    activation_key: str,
) -> None:
    interval = _compute_interval(activation_key)
    plan = build_usage_plan(interval, (), creator_generation=7)
    assert plan is not None
    app = _PlanningPool()
    app.interval = interval
    app.compute_activations[activation_key] = _compute_activation_row(activation_key)
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationDisabledError, match="source interval.*gate"):
        await materializer.freeze_plan(plan, 7)

    assert app.inserted_plan is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "enabled_resources", "expected"),
    [
        (
            {"resource": "agent_pod", "rate_usd": None},
            ("workspace_pod",),
            "publication event resource 'agent_pod'",
        ),
        (
            {"resource": "workspace_vm", "rate_usd": None},
            ("workspace_pod",),
            "publication event resource 'workspace_vm'",
        ),
        (
            {"details": {"product_class": "ide-session"}},
            ("workspace_pod",),
            "publication event IDE workspace Pod",
        ),
    ],
)
async def test_reviewed_correction_target_cannot_bypass_compute_class_gate(
    overrides: dict[str, Any],
    enabled_resources: tuple[str, ...],
    expected: str,
) -> None:
    original = _plan().events[0]
    changes_rate_selector = "resource" in overrides
    correction = build_correction_plan(
        _interval(),
        (
            CorrectionDelta(
                original=original,
                quantity=original.event.payload["quantity"],
                payload_overrides=overrides,
                inherit_rate=not changes_rate_selector,
            ),
        ),
        correction_reason="reviewed class repair",
        correction_actor_id=USER_ID,
        creator_generation=7,
        plan_revision=1,
    )
    app = _PlanningPool()
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        enabled_resources=enabled_resources,
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationDisabledError, match=expected):
        await materializer.freeze_plan(correction, 7)

    assert app.inserted_plan is None


def test_storage_candidate_selector_requires_exact_active_quantity_source() -> None:
    sql = materializer_sql._candidate_intervals_sql(storage_policy_enabled=True)
    legacy_sql = materializer_sql._candidate_intervals_sql(storage_policy_enabled=False)

    assert "storage_metering_source_requirements" in sql
    assert "storage_metering_source_activations" in sql
    assert "requirement_role = 'quantity'" in sql
    assert "storage_source_activation.state = 'active'" in sql
    assert "storage_global_activation.state = 'active'" in sql
    assert "unnest($5::text[], $6::text[], $7::text[])" in sql
    assert "storage_scope.id = interval.inventory_scope_id" in sql
    assert "storage_scope.source_cluster = interval.source_cluster" in sql
    assert "storage_metering_source_requirements" not in legacy_sql
    assert "storage_metering_source_activations" not in legacy_sql
    assert "interval.measurement_basis NOT IN" in legacy_sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interval_remote", "policy_remote"),
    [(True, False), (False, True)],
)
async def test_storage_gate_for_one_source_cannot_authorize_the_other(
    interval_remote: bool,
    policy_remote: bool,
) -> None:
    interval = _storage_interval(remote=interval_remote)
    plan = build_usage_plan(interval, (), creator_generation=7)
    assert plan is not None
    app = _PlanningPool()
    app.interval = interval
    app.storage_source_fences[(interval["inventory_scope_id"], "claim-requested")] = (
        _storage_source_fence(remote=interval_remote)
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        enabled_resources=("workspace_pod", interval["resource"]),
        storage_publication_policy=StoragePublicationPolicy(
            (_storage_authority(remote=policy_remote),)
        ),
    )  # type: ignore[arg-type]

    with pytest.raises(
        PublicationDisabledError,
        match="storage source publication gate is disabled",
    ):
        await materializer.freeze_plan(plan, 7)

    assert app.inserted_plan is None


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [False, True])
async def test_exact_active_storage_source_can_freeze_and_select_candidates(
    remote: bool,
) -> None:
    interval = _storage_interval(remote=remote)
    app = _PlanningPool()
    app.interval = interval
    app.storage_source_fences[(interval["inventory_scope_id"], "claim-requested")] = (
        _storage_source_fence(remote=remote)
    )
    authority = _storage_authority(remote=remote)
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        batch_size=3,
        enabled_resources=("workspace_pod", interval["resource"]),
        storage_publication_policy=StoragePublicationPolicy((authority,)),
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1
    assert app.candidate_args == (
        ["workspace_pod", interval["resource"]],
        START,
        3,
        False,
        ["claim-requested"],
        [authority.collector_id],
        [authority.source_cluster],
    )
    assert app.inserted_plan is not None


@pytest.mark.asyncio
async def test_storage_plan_cannot_predate_effective_source_boundary() -> None:
    interval = _storage_interval()
    plan = build_usage_plan(interval, (), creator_generation=7)
    assert plan is not None
    app = _PlanningPool()
    app.interval = interval
    app.storage_source_fences[(LOCAL_STORAGE_SCOPE_ID, "claim-requested")] = (
        _storage_source_fence(source_activated_at=START + timedelta(minutes=1))
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        enabled_resources=("workspace_pod", "workspace_pvc"),
        storage_publication_policy=StoragePublicationPolicy((_storage_authority(),)),
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationDisabledError, match="predates.*activation"):
        await materializer.freeze_plan(plan, 7)

    assert app.inserted_plan is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fence", "expected"),
    [
        (None, "lacks an exact quantity activation requirement"),
        (
            _storage_source_fence(requirement_role="attribution"),
            "activation is not active",
        ),
        (
            _storage_source_fence(source_state="shadow"),
            "activation is not active",
        ),
        (
            _storage_source_fence(remote=True),
            "does not match its inventory authority",
        ),
    ],
)
async def test_storage_freeze_rejects_missing_or_wrong_source_activation(
    fence: dict[str, Any] | None,
    expected: str,
) -> None:
    interval = _storage_interval()
    plan = build_usage_plan(interval, (), creator_generation=7)
    assert plan is not None
    app = _PlanningPool()
    app.interval = interval
    if fence is not None:
        app.storage_source_fences[(LOCAL_STORAGE_SCOPE_ID, "claim-requested")] = fence
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        enabled_resources=("workspace_pod", "workspace_pvc"),
        storage_publication_policy=StoragePublicationPolicy((_storage_authority(),)),
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationDisabledError, match=expected):
        await materializer.freeze_plan(plan, 7)

    assert app.inserted_plan is None


@pytest.mark.asyncio
async def test_storage_correction_resource_override_cannot_evade_source_gate() -> None:
    interval = _storage_interval()
    original_plan = build_usage_plan(interval, (), creator_generation=7)
    assert original_plan is not None
    correction = build_correction_plan(
        interval,
        (
            CorrectionDelta(
                original=original_plan.events[0],
                quantity=original_plan.events[0].event.payload["quantity"],
                payload_overrides={
                    "resource": "vm_rootdisk_claim",
                    "rate_usd": None,
                },
                inherit_rate=False,
            ),
        ),
        correction_reason="reviewed root-disk classification",
        correction_actor_id=USER_ID,
        creator_generation=7,
        plan_revision=1,
    )
    app = _PlanningPool()
    app.interval = interval
    app.storage_source_fences[(LOCAL_STORAGE_SCOPE_ID, "claim-requested")] = (
        _storage_source_fence()
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        enabled_resources=(
            "workspace_pod",
            "workspace_pvc",
            "vm_rootdisk_claim",
        ),
        storage_publication_policy=StoragePublicationPolicy(
            (_storage_authority(remote=True),)
        ),
    )  # type: ignore[arg-type]

    with pytest.raises(
        PublicationDisabledError,
        match="storage source publication gate is disabled",
    ):
        await materializer.freeze_plan(correction, 7)

    assert app.inserted_plan is None


@pytest.mark.asyncio
async def test_plan_batch_freezes_app_manifest_without_audit_io() -> None:
    app = _PlanningPool()
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True, batch_size=17
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1
    assert app.candidate_args == (["workspace_pod"], START, 17, False)
    assert app.rate_args is not None
    assert app.rate_args[5] == ["vcpu-hour", "gib-hour"]
    assert app.inserted_plan is not None
    assert app.ensured_days == [START.date()]
    assert len(app.inserted_events) == 2
    assert all(
        event["event_payload"]["payload_hash"] == event["row_hash"]
        for event in app.inserted_events
    )
    assert audit.insert_calls == 0


@pytest.mark.asyncio
async def test_plan_batch_locks_every_priced_rate_before_freezing_manifest() -> None:
    app = _PlanningPool()
    rate = _rate(
        "81000000-0000-0000-0000-000000000008",
        "vcpu-hour",
        "0.25",
        START - timedelta(days=1),
    )
    app.rates = [rate]
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1
    assert app.inserted_plan is not None
    assert app.locked_rate_ids == [rate["id"]]


@pytest.mark.asyncio
async def test_plan_batch_rejects_sealed_day_without_late_discovery_evidence() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    ledger = UsageLedger(_AuditPool(), UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationConflictError, match="discovery evidence"):
        await materializer.plan_batch(7)

    assert app.inserted_plan is None
    assert app.ensured_days == [START.date()]


@pytest.mark.asyncio
async def test_plan_batch_degrades_sealed_day_and_freezes_late_usage() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    app.coverage_status = "complete"
    app.coverage_revision = "seal-v1:" + "a" * 64
    app.coverage_sequence = 1
    app.interval.update(
        {
            "discovery_snapshot_id": UUID("87000000-0000-0000-0000-000000000008"),
            "discovery_received_at": START + timedelta(days=2),
            "discovery_snapshot_complete": True,
            "discovery_manifest_state": "sealed",
        }
    )
    audit = _AuditPool()
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(audit, UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1 and plans[0].plan_kind == "late-usage"
    assert app.coverage_status == "partial"
    assert app.coverage_sequence == 2
    assert app.coverage_revision is not None
    assert app.coverage_revision.startswith("late-v1:")
    assert app.unknown_ranges[0]["reason"] == "late-usage-discovery"
    assert (
        app.unknown_ranges[0]["discovery_evidence"]["kind"]
        == "complete-inventory-sighting"
    )
    assert {event["event_kind"] for event in app.inserted_events} == {"late-usage"}
    assert audit.insert_calls == 0


@pytest.mark.asyncio
async def test_plan_batch_accepts_durable_watch_receipt_as_late_evidence() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    app.coverage_status = "complete"
    app.coverage_revision = "seal-v1:" + "b" * 64
    app.coverage_sequence = 1
    app.interval.update(
        {
            "discovery_watch_event_id": UUID("89000000-0000-0000-0000-000000000008"),
            "discovery_watch_session_id": UUID("8a000000-0000-0000-0000-000000000008"),
            "discovery_watch_received_at": START + timedelta(days=2),
            "discovery_watch_event_type": "deleted",
            "discovery_watch_action": "close",
            "discovery_watch_resource_version": "991",
        }
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1 and plans[0].plan_kind == "late-usage"
    evidence = plans[0].events[0].event.payload["details"]["discovery_evidence"]
    assert evidence == {
        "kind": "authenticated-watch-receipt",
        "watch_session_id": "8a000000-0000-0000-0000-000000000008",
        "event_id": "89000000-0000-0000-0000-000000000008",
        "received_at": "2026-08-07T23:30:00.000000Z",
        "event_type": "deleted",
        "mutation_action": "close",
        "resource_version": "991",
    }
    assert app.unknown_ranges[0]["discovery_evidence"] == evidence


@pytest.mark.asyncio
async def test_waived_gap_accepts_terminal_list_link_after_item_expiry() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    app.coverage_status = "partial"
    app.coverage_revision = "waiver-v1:" + "1" * 64
    app.coverage_sequence = 2
    app.unknown_ranges = [{"reason": "operator-waiver"}]
    app.interval.update(
        {
            "discovery_snapshot_id": UUID("92000000-0000-0000-0000-000000000008"),
            "discovery_received_at": START + timedelta(days=2),
            "discovery_snapshot_complete": True,
            "discovery_manifest_state": "items-expired",
        }
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1 and plans[0].plan_kind == "late-usage"
    evidence = plans[0].events[0].event.payload["details"]["discovery_evidence"]
    assert evidence == {
        "kind": "complete-inventory-sighting",
        "snapshot_id": "92000000-0000-0000-0000-000000000008",
        "received_at": "2026-08-07T23:30:00.000000Z",
        "manifest_state": "items-expired",
    }
    assert app.unknown_ranges[0] == {"reason": "operator-waiver"}
    assert app.unknown_ranges[1]["discovery_evidence"] == evidence


@pytest.mark.asyncio
async def test_waived_gap_accepts_terminal_not_applicable_watch_link() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    app.coverage_status = "partial"
    app.coverage_revision = "waiver-v1:" + "2" * 64
    app.coverage_sequence = 2
    app.unknown_ranges = [{"reason": "operator-waiver"}]
    app.interval.update(
        {
            "discovery_watch_event_id": UUID("93000000-0000-0000-0000-000000000008"),
            "discovery_watch_session_id": UUID("94000000-0000-0000-0000-000000000008"),
            "discovery_watch_received_at": START + timedelta(days=2),
            "discovery_watch_event_type": "modified",
            "discovery_watch_action": "not-applicable",
            "discovery_watch_resource_version": "1002",
        }
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1 and plans[0].plan_kind == "late-usage"
    evidence = plans[0].events[0].event.payload["details"]["discovery_evidence"]
    assert evidence["kind"] == "authenticated-watch-receipt"
    assert evidence["event_type"] == "modified"
    assert evidence["mutation_action"] == "not-applicable"
    assert app.unknown_ranges[0] == {"reason": "operator-waiver"}
    assert app.unknown_ranges[1]["discovery_evidence"] == evidence


@pytest.mark.asyncio
async def test_plan_batch_accepts_complete_absence_snapshot_as_late_evidence() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    app.coverage_status = "complete"
    app.coverage_revision = "seal-v1:" + "c" * 64
    app.coverage_sequence = 1
    app.interval.update(
        {
            "absence_snapshot_id": UUID("8b000000-0000-0000-0000-000000000008"),
            "absence_received_at": START + timedelta(days=2),
            "absence_manifest_state": "items-expired",
        }
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    evidence = plans[0].events[0].event.payload["details"]["discovery_evidence"]
    assert evidence["kind"] == "complete-inventory-absence"
    assert evidence["snapshot_id"] == "8b000000-0000-0000-0000-000000000008"


def _successor_revision_evidence() -> dict[str, Any]:
    scope_id = UUID("8c000000-0000-0000-0000-000000000008")
    successor_id = UUID("8d000000-0000-0000-0000-000000000008")
    boundary = START + timedelta(hours=1, minutes=30)
    return {
        "inventory_scope_id": scope_id,
        "successor_interval_id": successor_id,
        "successor_inventory_scope_id": scope_id,
        "successor_lifecycle_id": LIFECYCLE_ID,
        "successor_revision_no": 2,
        "successor_source_kind": "pod",
        "successor_source_uid": "pod-uid-1",
        "successor_started_at": boundary,
        "successor_start_time_source": "app-db-received",
        "successor_start_evidence_source": "observed-revision-boundary",
    }


@pytest.mark.asyncio
async def test_waived_gap_accepts_successor_list_revision_as_late_evidence() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    app.coverage_status = "partial"
    app.coverage_revision = "waiver-v1:" + "d" * 64
    app.coverage_sequence = 2
    app.unknown_ranges = [{"reason": "operator-waiver"}]
    app.interval.update(
        {
            **_successor_revision_evidence(),
            "successor_snapshot_id": UUID("8e000000-0000-0000-0000-000000000008"),
            "successor_snapshot_received_at": START + timedelta(days=2),
            "successor_snapshot_complete": True,
            "successor_snapshot_manifest_state": "items-expired",
        }
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1 and plans[0].plan_kind == "late-usage"
    evidence = plans[0].events[0].event.payload["details"]["discovery_evidence"]
    assert evidence == {
        "kind": "successor-complete-inventory-sighting",
        "successor_interval_id": "8d000000-0000-0000-0000-000000000008",
        "revision_boundary": "2026-08-06T01:00:00.000000Z",
        "snapshot_id": "8e000000-0000-0000-0000-000000000008",
        "received_at": "2026-08-07T23:30:00.000000Z",
        "manifest_state": "items-expired",
    }
    assert app.unknown_ranges[0] == {"reason": "operator-waiver"}
    assert app.unknown_ranges[1]["discovery_evidence"] == evidence


@pytest.mark.asyncio
async def test_waived_gap_accepts_successor_watch_revision_as_late_evidence() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    app.coverage_status = "partial"
    app.coverage_revision = "waiver-v1:" + "e" * 64
    app.coverage_sequence = 2
    app.unknown_ranges = [{"reason": "operator-waiver"}]
    boundary = START + timedelta(hours=1, minutes=30)
    app.interval.update(
        {
            **_successor_revision_evidence(),
            "successor_watch_event_id": UUID("8f000000-0000-0000-0000-000000000008"),
            "successor_watch_session_id": UUID("90000000-0000-0000-0000-000000000008"),
            "successor_watch_received_at": boundary,
            "successor_watch_event_type": "modified",
            "successor_watch_action": "revise",
            "successor_watch_resource_version": "1001",
        }
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    plans = await materializer.plan_batch(7)

    assert len(plans) == 1 and plans[0].plan_kind == "late-usage"
    evidence = plans[0].events[0].event.payload["details"]["discovery_evidence"]
    assert evidence == {
        "kind": "successor-authenticated-watch-receipt",
        "successor_interval_id": "8d000000-0000-0000-0000-000000000008",
        "revision_boundary": "2026-08-06T01:00:00.000000Z",
        "watch_session_id": "90000000-0000-0000-0000-000000000008",
        "event_id": "8f000000-0000-0000-0000-000000000008",
        "received_at": "2026-08-06T01:00:00.000000Z",
        "event_type": "modified",
        "mutation_action": "revise",
        "resource_version": "1001",
    }
    assert app.unknown_ranges[0] == {"reason": "operator-waiver"}
    assert app.unknown_ranges[1]["discovery_evidence"] == evidence
    assert (
        "candidate.source_lifecycle_id = interval.source_lifecycle_id"
        in materializer_sql._CANDIDATE_INTERVALS_SQL
    )


@pytest.mark.asyncio
async def test_late_successor_evidence_rejects_a_different_boundary() -> None:
    app = _PlanningPool()
    app.day_state = "sealed"
    app.coverage_status = "partial"
    app.coverage_revision = "waiver-v1:" + "f" * 64
    app.coverage_sequence = 2
    app.interval.update(
        {
            **_successor_revision_evidence(),
            "successor_started_at": START + timedelta(hours=1, minutes=31),
            "successor_snapshot_id": UUID("91000000-0000-0000-0000-000000000008"),
            "successor_snapshot_received_at": START + timedelta(days=2),
            "successor_snapshot_complete": True,
            "successor_snapshot_manifest_state": "sealed",
        }
    )
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationConflictError, match="revision boundary"):
        await materializer.plan_batch(7)

    assert app.inserted_plan is None
    assert (
        "event.affected_interval_id = successor.id"
        in materializer_sql._CANDIDATE_INTERVALS_SQL
    )


@pytest.mark.asyncio
async def test_freeze_correction_plan_checks_monotonic_revision_without_audit_io() -> (
    None
):
    original = _plan().events[0]
    correction = build_correction_plan(
        _interval(),
        (CorrectionDelta(original=original, quantity="-4"),),
        correction_reason="reviewed removal",
        correction_actor_id=USER_ID,
        creator_generation=7,
        plan_revision=1,
        plan_id=UUID("86000000-0000-0000-0000-000000000008"),
    )
    app = _PlanningPool()
    audit = _AuditPool()
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(audit, UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    await materializer.freeze_plan(correction, 7)

    assert app.inserted_plan is not None
    assert app.inserted_plan[3:6] == ("correction", 1, False)
    assert len(app.inserted_events) == 1
    assert audit.insert_calls == 0

    app.inserted_plan = None
    app.next_correction_revision = 2
    with pytest.raises(PublicationConflictError, match="no longer next"):
        await materializer.freeze_plan(correction, 7)
    assert app.inserted_plan is None


@pytest.mark.asyncio
async def test_freeze_correction_rejects_cumulative_over_reversal() -> None:
    correction = build_correction_plan(
        _interval(),
        (CorrectionDelta(original=_plan().events[0], quantity="-4"),),
        correction_reason="duplicate full reversal",
        correction_actor_id=USER_ID,
        creator_generation=7,
        plan_revision=1,
    )
    payload = correction.events[0].event.payload
    key = (
        payload["corrects_source"],
        payload["corrects_source_id"],
        payload["corrects_unit"],
        payload["corrects_ts"],
    )
    app = _PlanningPool()
    app.prior_negative_quantities[key] = Decimal("4")
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationConflictError, match="exceed original quantity"):
        await materializer.freeze_plan(correction, 7)

    assert app.inserted_plan is None


def _ten_vcpu_hour_original() -> tuple[dict[str, Any], FrozenPublicationPlan, Any]:
    started_at = datetime(2026, 8, 5, 10, tzinfo=UTC)
    ended_at = started_at + timedelta(hours=1, minutes=15)
    interval = _interval(
        started_at=started_at,
        materialized_through=started_at,
        ended_at=ended_at,
        last_seen_at=ended_at,
        last_confirmed_at=ended_at,
    )
    ordinary = build_usage_plan(interval, (), creator_generation=7)
    assert ordinary is not None
    original = next(
        item for item in ordinary.events if item.event.payload["unit"] == "vcpu-hour"
    )
    assert original.event.payload["quantity"] == "10"
    return interval, ordinary, original


@pytest.mark.asyncio
async def test_freeze_correction_accepts_cumulative_partial_reversal_within_original() -> (
    None
):
    interval, ordinary, original = _ten_vcpu_hour_original()
    correction = build_correction_plan(
        interval,
        (
            CorrectionDelta(
                original=original,
                quantity="-3",
            ),
        ),
        correction_reason="second staged partial reversal",
        correction_actor_id=USER_ID,
        creator_generation=7,
        plan_revision=2,
    )
    payload = correction.events[0].event.payload
    key = (
        payload["corrects_source"],
        payload["corrects_source_id"],
        payload["corrects_unit"],
        payload["corrects_ts"],
    )
    app = _PlanningPool()
    app.interval = interval
    app.original_plan = ordinary
    app.next_correction_revision = 2
    app.prior_negative_quantities[key] = Decimal("4")
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    await materializer.freeze_plan(correction, 7)

    assert app.inserted_plan is not None
    assert payload["details"]["corrects_quantity"] == "10"
    assert payload["source_capacity_value"] == "8000"
    assert app.locked_original_keys == [key]


@pytest.mark.asyncio
async def test_freeze_correction_rejects_cumulative_partial_over_reversal() -> None:
    interval, ordinary, original = _ten_vcpu_hour_original()
    correction = build_correction_plan(
        interval,
        (
            CorrectionDelta(
                original=original,
                quantity="-7",
            ),
        ),
        correction_reason="excess staged partial reversal",
        correction_actor_id=USER_ID,
        creator_generation=7,
        plan_revision=2,
    )
    payload = correction.events[0].event.payload
    key = (
        payload["corrects_source"],
        payload["corrects_source_id"],
        payload["corrects_unit"],
        payload["corrects_ts"],
    )
    app = _PlanningPool()
    app.interval = interval
    app.original_plan = ordinary
    app.next_correction_revision = 2
    app.prior_negative_quantities[key] = Decimal("4")
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationConflictError, match="exceed original quantity"):
        await materializer.freeze_plan(correction, 7)

    assert app.inserted_plan is None


@pytest.mark.asyncio
async def test_freeze_correction_rejects_duplicate_reversals_in_one_plan() -> None:
    original = _plan().events[0]
    correction = build_correction_plan(
        _interval(),
        (
            CorrectionDelta(original=original, quantity="-4"),
            CorrectionDelta(original=original, quantity="-4"),
        ),
        correction_reason="duplicate reversal rows",
        correction_actor_id=USER_ID,
        creator_generation=7,
        plan_revision=1,
    )
    app = _PlanningPool()
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(_AuditPool(), UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationConflictError, match="exceed original quantity"):
        await materializer.freeze_plan(correction, 7)

    assert app.inserted_plan is None


@pytest.mark.asyncio
async def test_create_correction_verifies_original_before_freezing_group() -> None:
    app = _PlanningPool()
    original = app.original_plan.events[0]
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    await ledger.publish_frozen_events([original.event])
    insert_calls_before = audit.insert_calls
    materializer = InfrastructureUsageMaterializer(
        app,
        ledger,
        publication_enabled=True,
    )  # type: ignore[arg-type]
    replacement_user = UUID("88000000-0000-0000-0000-000000000008")
    base_request = {
        "source": original.event.payload["source"],
        "source_id": original.event.payload["source_id"],
        "unit": original.event.payload["unit"],
        "ts": original.event.timestamp,
        "expected_payload_hash": original.event.row_hash,
    }

    correction = await materializer.create_correction(
        7,
        (
            CorrectionRequestDelta(quantity="-4", **base_request),
            CorrectionRequestDelta(
                quantity="4",
                payload_overrides={"user_id": replacement_user},
                **base_request,
            ),
        ),
        correction_reason="reviewed owner repair",
        correction_actor_id=USER_ID,
    )

    assert correction.plan_revision == 1
    assert correction.plan_kind == "correction"
    assert len(correction.events) == 2
    assert correction.events[1].event.payload["user_id"] == str(replacement_user)
    assert app.inserted_plan is not None
    # Original verification is strictly read-only; only the explicit fixture
    # publication above touched the audit ledger.
    assert audit.insert_calls == insert_calls_before


@pytest.mark.asyncio
async def test_create_correction_validates_replacement_rate_version() -> None:
    app = _PlanningPool()
    original = app.original_plan.events[0]
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    await ledger.publish_frozen_events([original.event])
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]
    rate_id = UUID("8c000000-0000-0000-0000-000000000008")
    request = CorrectionRequestDelta(
        source=original.event.payload["source"],
        source_id=original.event.payload["source_id"],
        unit=original.event.payload["unit"],
        ts=original.event.timestamp,
        expected_payload_hash=original.event.row_hash,
        quantity="4",
        payload_overrides={"rate_usd": "0.25"},
        inherit_rate=False,
        canonical_rate_version_id=rate_id,
    )

    with pytest.raises(PublicationConflictError, match="immutable rate version"):
        await materializer.create_correction(
            7,
            (request,),
            correction_reason="reviewed price repair",
            correction_actor_id=USER_ID,
        )
    assert app.inserted_plan is None

    payload = original.event.payload
    app.replacement_rates = [
        {
            "id": rate_id,
            "unit": payload["unit"],
            "usd_per_unit": "0.25",
            "cost_domain": payload["cost_domain"],
            "measurement_basis": payload["measurement_basis"],
            "category": payload["category"],
            "resource_class": payload["resource_class"],
            "resource": payload["resource"],
            "effective_from": START - timedelta(days=1),
            "effective_to": START + timedelta(days=2),
        }
    ]
    plan = await materializer.create_correction(
        7,
        (request,),
        correction_reason="reviewed price repair",
        correction_actor_id=USER_ID,
    )
    assert plan.events[0].canonical_rate_version_id == rate_id
    assert plan.events[0].event.payload["rate_usd"] == "0.25"
    assert app.locked_rate_ids == [rate_id]


@pytest.mark.asyncio
async def test_create_correction_idempotency_replays_exact_frozen_intent() -> None:
    app = _PlanningPool()
    original = app.original_plan.events[0]
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    await ledger.publish_frozen_events([original.event])
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]
    correction_id = UUID("8d000000-0000-0000-0000-000000000008")
    request = CorrectionRequestDelta(
        source=original.event.payload["source"],
        source_id=original.event.payload["source_id"],
        unit=original.event.payload["unit"],
        ts=original.event.timestamp,
        expected_payload_hash=original.event.row_hash,
        quantity="-4",
    )

    first = await materializer.create_correction(
        7,
        (request,),
        correction_reason="reviewed duplicate-safe repair",
        correction_actor_id=USER_ID,
        correction_id=correction_id,
    )
    writes_before_replay = (
        app.plan_insert_calls,
        copy.deepcopy(app.inserted_plan),
        copy.deepcopy(app.inserted_events),
        audit.insert_calls,
        copy.deepcopy(audit.rows),
    )
    replay = await materializer.create_correction(
        7,
        (request,),
        correction_reason="reviewed duplicate-safe repair",
        correction_actor_id=USER_ID,
        correction_id=correction_id,
    )

    assert replay.id == first.id == correction_id
    assert replay.event_set_hash == first.event_set_hash
    assert app.plan_insert_calls == 1
    assert (
        app.plan_insert_calls,
        app.inserted_plan,
        app.inserted_events,
        audit.insert_calls,
        audit.rows,
    ) == writes_before_replay

    with pytest.raises(PublicationConflictError, match="changed immutable intent"):
        await materializer.create_correction(
            7,
            (request,),
            correction_reason="changed reason",
            correction_actor_id=USER_ID,
            correction_id=correction_id,
        )


def _plan_row(plan: Any) -> dict[str, Any]:
    return {
        "id": plan.id,
        "source_interval_id": plan.source_interval_id,
        "source_revision": plan.source_revision,
        "plan_kind": plan.plan_kind,
        "plan_revision": plan.plan_revision,
        "advances_cursor": plan.advances_cursor,
        "previous_materialized_through": plan.previous_materialized_through,
        "correction_group_id": plan.correction_group_id,
        "period_start": plan.period_start,
        "period_end": plan.period_end,
        "expected_event_count": len(plan.events),
        "payload_schema_version": plan.payload_schema_version,
        "event_set_hash": plan.event_set_hash,
        "rate_selection_hash": plan.rate_selection_hash,
        "creator_generation": plan.creator_generation,
        "state": plan.state,
        "attempt_count": 0,
        "sanitized_error": None,
    }


def _event_rows(plan: Any) -> list[dict[str, Any]]:
    return [
        {
            "ordinal": item.ordinal,
            "canonical_rate_version_id": item.canonical_rate_version_id,
            "row_hash": item.event.row_hash,
            "event_payload": dict(item.event.payload),
        }
        for item in plan.events
    ]


class _AppTransaction:
    def __init__(self, connection: _AppConnection):
        self.connection = connection
        self.before: Any = None

    async def __aenter__(self):
        pool = self.connection.pool
        self.before = copy.deepcopy((pool.plan, pool.cursor))
        return self

    async def __aexit__(self, exc_type: Any, *_args: Any):
        if exc_type is not None:
            self.connection.pool.plan, self.connection.pool.cursor = self.before
        return False


class _AppConnection:
    def __init__(self, pool: _AppPool):
        self.pool = pool

    def transaction(self, **_kwargs: Any):
        return _AppTransaction(self)

    async def fetch(self, sql: str, *args: Any):
        if "infra-publication:plan-events" in sql:
            return copy.deepcopy(self.pool.events)
        if "infra-publication:compute-epoch-set-lock" in sql:
            return [
                {"activation_key": key, "id": COMPUTE_EPOCH_ID}
                for key in args[0]
                if (key, COMPUTE_SCOPE_ID) in self.pool.compute_exact_epochs
            ]
        raise AssertionError(f"unexpected app fetch: {sql}")

    async def fetchrow(self, sql: str, *args: Any):
        if "infra-publication:control" in sql:
            return {
                "leader_generation": self.pool.generation,
                "cutover_state": self.pool.cutover_state,
                "cutover_at": START,
            }
        if "infra-publication:pending-plan" in sql:
            return (
                copy.deepcopy(self.pool.plan)
                if self.pool.plan["state"] == "planned"
                else None
            )
        if "infra-publication:publication-interval-fence" in sql:
            if args != (
                self.pool.plan["source_interval_id"],
                self.pool.plan["source_revision"],
            ):
                return None
            return copy.deepcopy(self.pool.interval)
        if "infra-publication:compute-activation-fence" in sql:
            return copy.deepcopy(self.pool.compute_activations.get(str(args[0])))
        if "infra-publication:compute-exact-epoch-fence" in sql:
            if args[2] != COMPUTE_EPOCH_ID:
                return None
            return copy.deepcopy(
                self.pool.compute_exact_epochs.get((str(args[0]), args[1]))
            )
        if "infra-publication:storage-source-fence" in sql:
            return copy.deepcopy(
                self.pool.storage_source_fences.get((args[0], str(args[1])))
            )
        if "infra-publication:lock-plan" in sql:
            return {"state": self.pool.plan["state"]}
        if "infra-publication:advance-cursor" in sql:
            interval_id, revision, previous, target = args
            if (
                interval_id != self.pool.plan["source_interval_id"]
                or revision != self.pool.plan["source_revision"]
                or previous != self.pool.cursor
            ):
                return None
            self.pool.cursor = target
            return {"materialized_through": target}
        if "infra-publication:publish-plan" in sql:
            if self.pool.plan["state"] != "planned":
                return None
            self.pool.plan["state"] = "published"
            self.pool.plan["attempt_count"] += 1
            self.pool.plan["sanitized_error"] = None
            return {"id": args[0]}
        if "infra-publication:finalize-conflict" in sql:
            if self.pool.plan["state"] != "planned":
                return None
            self.pool.plan["state"] = "conflict"
            self.pool.plan["attempt_count"] += 1
            self.pool.plan["sanitized_error"] = json.loads(args[1])
            return {"id": args[0]}
        if "infra-publication:record-failure" in sql:
            plan_id, generation, state, raw_error = args
            if (
                plan_id != self.pool.plan["id"]
                or generation != self.pool.generation
                or self.pool.cutover_state != "active"
                or self.pool.plan["state"] != "planned"
            ):
                return None
            self.pool.plan["state"] = state
            self.pool.plan["attempt_count"] += 1
            self.pool.plan["sanitized_error"] = json.loads(raw_error)
            return {"state": state}
        raise AssertionError(f"unexpected app fetchrow: {sql}")


class _AppPool:
    def __init__(self, plan: Any, *, interval: dict[str, Any] | None = None):
        self.plan = _plan_row(plan)
        self.events = _event_rows(plan)
        self.interval = copy.deepcopy(interval or _interval())
        self.cursor = plan.previous_materialized_through
        self.generation = 7
        self.cutover_state = "active"
        self.compute_activations: dict[str, dict[str, Any]] = {}
        self.compute_exact_epochs: dict[tuple[str, UUID], dict[str, Any]] = {}
        resource = self.interval.get("resource")
        details = self.interval.get("details") or {}
        activation_key = (
            "agent_pod"
            if resource == "agent_pod"
            else "workspace_vm"
            if resource == "workspace_vm"
            else "ide_workspace_pod"
            if resource == "workspace_pod"
            and isinstance(details, dict)
            and details.get("product_class") == "ide-session"
            else None
        )
        if activation_key is not None:
            self.compute_exact_epochs[(activation_key, COMPUTE_SCOPE_ID)] = (
                _compute_exact_epoch_row()
            )
        self.storage_source_fences: dict[tuple[UUID, str], dict[str, Any]] = {}

    def acquire(self):
        return _Acquire(_AppConnection(self))


@pytest.mark.asyncio
async def test_pending_ide_plan_rechecks_subtype_gate_before_audit() -> None:
    interval = _compute_interval("ide_workspace_pod")
    plan = build_usage_plan(interval, (), creator_generation=7)
    assert plan is not None
    app = _AppPool(plan, interval=interval)
    app.compute_activations["ide_workspace_pod"] = _compute_activation_row(
        "ide_workspace_pod"
    )
    audit = _AuditPool()
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(audit, UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        ide_workspace_pod_enabled=False,
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationDisabledError, match="IDE workspace Pod"):
        await materializer.publish_one(7)

    assert audit.insert_calls == 0
    assert app.plan["state"] == "planned"


@pytest.mark.asyncio
async def test_pending_storage_plan_rechecks_exact_source_before_audit() -> None:
    interval = _storage_interval(remote=True)
    plan = build_usage_plan(interval, (), creator_generation=7)
    assert plan is not None
    app = _AppPool(plan, interval=interval)
    app.storage_source_fences[(REMOTE_STORAGE_SCOPE_ID, "claim-requested")] = (
        _storage_source_fence(remote=True)
    )
    audit = _AuditPool()
    materializer = InfrastructureUsageMaterializer(
        app,
        UsageLedger(audit, UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        enabled_resources=("workspace_pod", "vm_rootdisk_claim"),
        storage_publication_policy=StoragePublicationPolicy(
            (_storage_authority(remote=False),)
        ),
    )  # type: ignore[arg-type]

    with pytest.raises(
        PublicationDisabledError,
        match="storage source publication gate is disabled",
    ):
        await materializer.publish_one(7)

    assert audit.insert_calls == 0
    assert app.plan["state"] == "planned"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("activation_key", "resource"),
    [
        ("agent_pod", "agent_pod"),
        ("workspace_vm", "workspace_vm"),
    ],
)
async def test_pending_compute_plan_requires_class_gate_and_effective_activation(
    activation_key: str,
    resource: str,
) -> None:
    interval = _compute_interval(activation_key)
    plan = build_usage_plan(interval, (), creator_generation=7)
    assert plan is not None

    gated_app = _AppPool(plan, interval=interval)
    gated_app.compute_activations[activation_key] = _compute_activation_row(
        activation_key
    )
    gated_audit = _AuditPool()
    gated = InfrastructureUsageMaterializer(
        gated_app,
        UsageLedger(gated_audit, UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
    )  # type: ignore[arg-type]
    with pytest.raises(PublicationDisabledError, match="publication gate"):
        await gated.publish_one(7)
    assert gated_audit.insert_calls == 0

    shadow_app = _AppPool(plan, interval=interval)
    shadow_app.compute_activations[activation_key] = _compute_activation_row(
        activation_key,
        state="shadow",
    )
    shadow_audit = _AuditPool()
    shadow = InfrastructureUsageMaterializer(
        shadow_app,
        UsageLedger(shadow_audit, UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        enabled_resources=("workspace_pod", resource),
    )  # type: ignore[arg-type]
    with pytest.raises(PublicationDisabledError, match="activation is not active"):
        await shadow.publish_one(7)
    assert shadow_audit.insert_calls == 0

    future_app = _AppPool(plan, interval=interval)
    future_app.compute_activations[activation_key] = _compute_activation_row(
        activation_key,
        activated_at=START + timedelta(minutes=1),
    )
    future_audit = _AuditPool()
    future = InfrastructureUsageMaterializer(
        future_app,
        UsageLedger(future_audit, UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        enabled_resources=("workspace_pod", resource),
    )  # type: ignore[arg-type]
    with pytest.raises(PublicationDisabledError, match="predates activation"):
        await future.publish_one(7)
    assert future_audit.insert_calls == 0

    active_app = _AppPool(plan, interval=interval)
    active_app.compute_activations[activation_key] = _compute_activation_row(
        activation_key
    )
    active_audit = _AuditPool()
    active = InfrastructureUsageMaterializer(
        active_app,
        UsageLedger(active_audit, UsageRates(None)),  # type: ignore[arg-type]
        publication_enabled=True,
        enabled_resources=("workspace_pod", resource),
    )  # type: ignore[arg-type]

    result = await active.publish_one(7)

    assert result is not None
    assert active_audit.insert_calls == 1
    assert active_app.plan["state"] == "published"


@pytest.mark.asyncio
async def test_materializer_gate_is_independent_and_off_by_default() -> None:
    plan = _plan()
    app = _AppPool(plan)
    ledger = UsageLedger(_AuditPool(), UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(app, ledger)  # type: ignore[arg-type]

    with pytest.raises(PublicationDisabledError, match="runtime gate"):
        await materializer.publish_one(7)


@pytest.mark.asyncio
async def test_materializer_replays_committed_audit_batch_then_advances_once() -> None:
    plan = _plan()
    app = _AppPool(plan)
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    events = [item.event for item in plan.events]
    committed = await ledger.publish_frozen_events(events)
    assert committed.inserted == 2
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    result = await materializer.publish_one(7)

    assert result is not None
    assert result.audit.inserted == 0
    assert result.audit.verified == 2
    assert result.cursor_advanced
    assert app.cursor == plan.period_end
    assert app.plan["state"] == "published"
    assert app.plan["attempt_count"] == 1


@pytest.mark.asyncio
async def test_materializer_publishes_correction_group_without_moving_cursor() -> None:
    original = _plan().events[0]
    correction = build_correction_plan(
        _interval(),
        (CorrectionDelta(original=original, quantity="-4"),),
        correction_reason="reviewed capacity removal",
        correction_actor_id=USER_ID,
        creator_generation=7,
        plan_revision=1,
        plan_id=UUID("85000000-0000-0000-0000-000000000008"),
    )
    app = _AppPool(correction)
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    result = await materializer.publish_one(7)

    assert result is not None
    assert not result.cursor_advanced
    assert app.cursor is None
    assert app.plan["state"] == "published"
    assert len(audit.rows) == 1


@pytest.mark.asyncio
async def test_failed_app_cursor_cas_becomes_terminal_visible_conflict() -> None:
    plan = _plan()
    app = _AppPool(plan)
    app.cursor = plan.period_start + timedelta(seconds=1)
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationConflictError, match="cursor changed"):
        await materializer.publish_one(7)

    assert len(audit.rows) == 2
    assert app.plan["state"] == "conflict"
    assert app.plan["attempt_count"] == 1
    assert app.plan["sanitized_error"] == {"code": "interval-cursor-conflict"}
    assert app.cursor == plan.period_start + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_materializer_fences_stale_generation_before_audit_io() -> None:
    plan = _plan()
    app = _AppPool(plan)
    app.generation = 8
    audit = _AuditPool()
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(PublicationFenceError, match="stale"):
        await materializer.publish_one(7)

    assert audit.insert_calls == 0
    assert app.plan["state"] == "planned"


@pytest.mark.asyncio
async def test_materializer_marks_audit_hash_conflict_terminal() -> None:
    plan = _plan()
    app = _AppPool(plan)
    audit = _AuditPool()
    audit.rows[plan.events[0].event.dedupe_key] = "f" * 64
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(StrictUsageConflict):
        await materializer.publish_one(7)

    assert app.plan["state"] == "conflict"
    assert app.plan["attempt_count"] == 1
    assert app.plan["sanitized_error"] == {"code": "audit-payload-conflict"}
    assert app.cursor == plan.period_start


@pytest.mark.asyncio
async def test_materializer_keeps_missing_partition_plan_pending() -> None:
    plan = _plan()
    app = _AppPool(plan)
    audit = _AuditPool(attached={"usage_events_p2026_07"})
    ledger = UsageLedger(audit, UsageRates(None))  # type: ignore[arg-type]
    materializer = InfrastructureUsageMaterializer(
        app, ledger, publication_enabled=True
    )  # type: ignore[arg-type]

    with pytest.raises(StrictUsagePartitionMissing):
        await materializer.publish_one(7)

    assert app.plan["state"] == "planned"
    assert app.plan["attempt_count"] == 1
    assert app.plan["sanitized_error"] == {
        "code": "audit-partition-missing",
        "partitions": ["usage_events_p2026_08"],
    }
    assert app.cursor == plan.period_start
