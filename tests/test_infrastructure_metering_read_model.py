"""Source-aware infrastructure usage read-model contract tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from orchestrator.services.infrastructure_metering.materializer import (
    CorrectionDelta,
    FrozenPublicationPlan,
    StoragePublicationAuthority,
    StoragePublicationPolicy,
    build_correction_plan,
    build_late_usage_plan,
    build_usage_plan,
)
from orchestrator.services.infrastructure_metering.queries import UsageVisibility
from orchestrator.services.infrastructure_metering.read_model import (
    AppUsageReadSnapshot,
    SourceAwareUsageReadModel,
    UsageReadContractError,
    UsageReadCutoverInactive,
)


UTC = timezone.utc
START = datetime(2026, 8, 5, 10, tzinfo=UTC)
END = START + timedelta(hours=1)
INTERVAL_ID = UUID("10000000-0000-0000-0000-000000000001")
LIFECYCLE_ID = UUID("20000000-0000-0000-0000-000000000002")
OWNER_ID = UUID("30000000-0000-0000-0000-000000000003")
USER_ID = UUID("40000000-0000-0000-0000-000000000004")
PROJECT_ID = UUID("50000000-0000-0000-0000-000000000005")
SCOPE_ID = UUID("60000000-0000-0000-0000-000000000006")
EPOCH_ID = UUID("70000000-0000-0000-0000-000000000007")
NEW_OWNER_ID = UUID("31000000-0000-0000-0000-000000000003")
NEW_USER_ID = UUID("41000000-0000-0000-0000-000000000004")
NEW_PROJECT_ID = UUID("51000000-0000-0000-0000-000000000005")
COVERAGE_REVISION = "coverage-revision-1"


def _storage_policy(
    *authorities: tuple[str, str, str],
) -> StoragePublicationPolicy:
    return StoragePublicationPolicy(
        authorities=tuple(
            StoragePublicationAuthority(
                measurement_basis=basis,
                collector_id=collector_id,
                source_cluster=source_cluster,
            )
            for basis, collector_id, source_cluster in authorities
        )
    )


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
        "ended_at": None,
        "end_time_source": None,
        "end_uncertainty_us": None,
        "last_seen_at": END,
        "last_confirmed_at": END,
        "materialized_through": START,
        "end_reason": None,
    }
    row.update(overrides)
    return row


def _rate(
    rate_id: str,
    unit: str,
    usd_per_unit: str,
    *,
    effective_from: datetime = START - timedelta(days=1),
) -> dict[str, Any]:
    return {
        "id": UUID(rate_id),
        "unit": unit,
        "usd_per_unit": Decimal(usd_per_unit),
        "effective_from": effective_from,
        "effective_to": None,
    }


def _snapshot(
    *,
    cutover_at: datetime = START,
    watermark: date | None = None,
    rolled_days: tuple[date, date] | None = None,
    daily_rows: tuple[dict[str, Any], ...] = (),
    intervals: tuple[dict[str, Any], ...] = (),
    plans: tuple[FrozenPublicationPlan, ...] = (),
    rate_rows: tuple[dict[str, Any], ...] = (),
    epochs: tuple[dict[str, Any], ...] = (),
    storage_requirements: tuple[dict[str, Any], ...] = (),
    compute_requirements: tuple[dict[str, Any], ...] = (),
    gaps: tuple[dict[str, Any], ...] = (),
    day_states: tuple[dict[str, Any], ...] = (),
) -> AppUsageReadSnapshot:
    return AppUsageReadSnapshot(
        cutover_at=cutover_at,
        watermark=watermark,
        rolled_days=rolled_days,
        daily_rows=daily_rows,
        intervals=intervals,
        plans=plans,
        rate_rows=rate_rows,
        epochs=epochs,
        storage_requirements=storage_requirements,
        gaps=gaps,
        day_states=day_states,
        compute_requirements=compute_requirements,
    )


def _usage_row(
    *,
    priced_quantity: str = "0",
    unpriced_quantity: str = "8",
    cost_usd: str | None = None,
    priced_events: int = 0,
    unpriced_events: int = 1,
) -> dict[str, Any]:
    return {
        "category": "compute",
        "resource": "workspace_pod",
        "unit": "vcpu-hour",
        "measurement_basis": "scheduler-request",
        "resource_class": "kubernetes-pod",
        "attribution_scope": "customer",
        "cost_domain": "workload-allocation",
        "measurement_algorithm": "kubernetes-pod-requests-v1",
        "priced_quantity": Decimal(priced_quantity),
        "unpriced_quantity": Decimal(unpriced_quantity),
        "cost_usd": None if cost_usd is None else Decimal(cost_usd),
        "priced_events": priced_events,
        "unpriced_events": unpriced_events,
        "events": priced_events + unpriced_events,
    }


def _epoch(
    *,
    required_from: datetime = START,
    complete_through: datetime = END,
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": EPOCH_ID,
        "scope_id": SCOPE_ID,
        "required_from": required_from,
        "retired_at": None,
        "complete_through": complete_through,
        "snapshot_health": "healthy",
        "continuity_health": "healthy",
        "item_health": "healthy",
        "backend_health": "healthy",
        "publication_health": "healthy",
        "api_resource": "core/v1/pods",
        "collector_id": "kubernetes-pods",
        "source_cluster": "main-dev",
        "namespace": "srw",
    }
    row.update(overrides)
    return row


def _storage_requirement(
    *,
    measurement_basis: str = "claim-requested",
    collector_id: str = "kubernetes-pods",
    source_cluster: str = "main-dev",
    inventory_scope_id: UUID = SCOPE_ID,
    requirement_role: str = "quantity",
    effective_from: datetime = START,
) -> dict[str, Any]:
    return {
        "measurement_basis": measurement_basis,
        "collector_id": collector_id,
        "source_cluster": source_cluster,
        "inventory_scope_id": inventory_scope_id,
        "requirement_role": requirement_role,
        "effective_from": effective_from,
    }


def _compute_requirement(
    *,
    activation_key: str = "agent_pod",
    epoch_id: UUID = EPOCH_ID,
    retired_at: datetime | None = None,
    authority_sequence: int = 1,
    authority_effective_from: datetime = START,
    complete_through: datetime | None = None,
) -> dict[str, Any]:
    return {
        "activation_key": activation_key,
        "activated_at": START,
        "inventory_scope_id": SCOPE_ID,
        "inventory_scope_epoch_id": epoch_id,
        "authority_sequence": authority_sequence,
        "authority_effective_from": authority_effective_from,
        "retired_at": retired_at,
        "complete_through": complete_through or retired_at or END,
        "snapshot_health": "healthy",
        "continuity_health": "healthy",
        "item_health": "healthy",
        "backend_health": "healthy",
        "api_resource": "core/v1/pods",
        "collector_id": "kubernetes-pods",
        "source_cluster": "main-dev",
        "namespace": "srw",
    }


def _day_state(
    day: date,
    *,
    coverage_status: str = "complete",
    unknown_ranges: list[dict[str, Any]] | None = None,
    infra_state: str = "sealed",
    rollup_revision: str | None = COVERAGE_REVISION,
    current_revision: str | None = COVERAGE_REVISION,
) -> dict[str, Any]:
    return {
        "day": day,
        "coverage_status": coverage_status,
        "unknown_ranges": unknown_ranges or [],
        "infra_coverage_revision": rollup_revision,
        "infra_state": infra_state,
        "current_infra_coverage_revision": current_revision,
    }


def _published_plan(
    *, rates: tuple[dict[str, Any], ...] = ()
) -> tuple[dict[str, Any], FrozenPublicationPlan]:
    source = _interval(ended_at=END, end_reason="deleted")
    plan = build_usage_plan(source, rates, creator_generation=1)
    assert plan is not None
    return (
        {**source, "materialized_through": END},
        replace(plan, state="published"),
    )


class _Acquire:
    def __init__(self, connection: Any):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args: Any):
        return False


class _AppTransaction:
    def __init__(self, connection: _AppConnection, options: dict[str, Any]):
        self.connection = connection
        self.options = options

    async def __aenter__(self):
        assert not self.connection.in_transaction
        self.connection.in_transaction = True
        self.connection.transaction_options.append(self.options)
        return self

    async def __aexit__(self, *_args: Any):
        self.connection.in_transaction = False
        return False


class _AppConnection:
    def __init__(self, control: dict[str, Any] | None):
        self.control = control
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.transaction_options: list[dict[str, Any]] = []
        self.in_transaction = False

    def transaction(self, **options: Any):
        return _AppTransaction(self, options)

    def _record(self, operation: str, sql: str, params: tuple[Any, ...]) -> None:
        assert self.in_transaction, "all app reads must share the snapshot transaction"
        self.calls.append((operation, sql, params))

    async def fetchrow(self, sql: str, *params: Any):
        self._record("fetchrow", sql, params)
        if "infra-read:control" in sql:
            return self.control
        raise AssertionError(f"unexpected app fetchrow: {sql}")

    async def fetch(self, sql: str, *params: Any):
        self._record("fetch", sql, params)
        if any(
            marker in sql
            for marker in (
                "infra-read:daily",
                "infra-read:intervals",
                "infra-read:plan-headers",
                "infra-read:correction-plan-headers",
                "infra-read:plan-events",
                "infra-read:rates",
                "infra-read:epochs",
                "infra-read:storage-requirements",
                "infra-read:gaps",
                "infra-read:storage-gaps",
                "infra-read:day-states",
            )
        ):
            return []
        raise AssertionError(f"unexpected app fetch: {sql}")


class _AppPool:
    def __init__(self, control: dict[str, Any] | None):
        self.connection = _AppConnection(control)

    def acquire(self):
        return _Acquire(self.connection)


class _AuditPool:
    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *params: Any):
        self.calls.append((sql, params))
        return self.rows


async def _summarize(
    snapshot: AppUsageReadSnapshot,
    *,
    from_ts: datetime = START,
    to_ts: datetime = END,
    audit_rows: list[dict[str, Any]] | None = None,
    visibility: UsageVisibility | None = None,
    ref_id: str | None = None,
    enabled_resources: tuple[str, ...] = ("workspace_pod",),
    storage_publication_policy: StoragePublicationPolicy | None = None,
):
    audit = _AuditPool(audit_rows)
    model = SourceAwareUsageReadModel(
        audit,
        _AppPool(None),
        enabled_resources=enabled_resources,
        storage_publication_policy=storage_publication_policy,
    )
    model.read_app_snapshot = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]
    result = await model.summary(
        from_ts=from_ts,
        to_ts=to_ts,
        visibility=visibility or UsageVisibility(),
        ref_id=ref_id,
        as_of=to_ts,
    )
    return result, audit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "control",
    [
        {"cutover_state": "shadow", "cutover_at": START, "last_closed_day": None},
        {"cutover_state": "active", "cutover_at": None, "last_closed_day": None},
    ],
)
async def test_read_model_fails_closed_until_durable_cutover_is_active(control):
    app = _AppPool(control)
    audit = _AuditPool()
    model = SourceAwareUsageReadModel(audit, app)

    with pytest.raises(UsageReadCutoverInactive):
        await model.summary(
            from_ts=START,
            to_ts=END,
            visibility=UsageVisibility(),
            as_of=END,
        )

    assert audit.calls == []


@pytest.mark.asyncio
async def test_app_sources_share_one_readonly_repeatable_read_snapshot():
    app = _AppPool(
        {
            "cutover_state": "active",
            "cutover_at": START,
            "last_closed_day": None,
        }
    )
    model = SourceAwareUsageReadModel(_AuditPool(), app)

    snapshot = await model.read_app_snapshot(
        from_ts=START,
        to_ts=END,
        visibility=UsageVisibility(),
    )

    assert snapshot.cutover_at == START
    assert app.connection.transaction_options == [
        {"isolation": "repeatable_read", "readonly": True}
    ]
    assert not app.connection.in_transaction
    assert {
        marker
        for _operation, sql, _params in app.connection.calls
        for marker in (
            "control",
            "intervals",
            "plan-headers",
            "plan-events",
            "rates",
            "epochs",
        )
        if f"infra-read:{marker}" in sql
    } == {"control", "intervals", "plan-headers", "plan-events", "rates", "epochs"}


@pytest.mark.asyncio
async def test_epoch_queries_begin_after_the_immutable_rollup_boundary():
    from_ts = datetime(2026, 8, 5, tzinfo=UTC)
    rolled_cutoff = datetime(2026, 8, 6, tzinfo=UTC)
    to_ts = rolled_cutoff + timedelta(hours=1)
    app = _AppPool(
        {
            "cutover_state": "active",
            "cutover_at": from_ts,
            "last_closed_day": date(2026, 8, 5),
        }
    )
    model = SourceAwareUsageReadModel(_AuditPool(), app)

    await model.read_app_snapshot(
        from_ts=from_ts,
        to_ts=to_ts,
        visibility=UsageVisibility(),
    )

    _operation, _sql, params = next(
        call for call in app.connection.calls if "infra-read:epochs" in call[1]
    )
    assert params == (rolled_cutoff, to_ts)


@pytest.mark.asyncio
async def test_storage_reads_include_asset_gaps_only_for_enabled_pv_resources():
    control = {
        "cutover_state": "active",
        "cutover_at": START,
        "last_closed_day": None,
    }
    app = _AppPool(control)
    app.connection.fetch = AsyncMock(
        side_effect=app.connection.fetch,
    )
    model = SourceAwareUsageReadModel(
        _AuditPool(),
        app,
        enabled_resources=("workspace_pod", "block_volume_local_path"),
    )

    # Make the epoch query return one PV epoch so the gap query is exercised.
    original_fetch = app.connection.fetch.side_effect

    async def fetch(sql: str, *params: Any):
        if "infra-read:epochs" in sql:
            app.connection._record("fetch", sql, params)
            return [_epoch(api_resource="core/v1/persistentvolumes")]
        return await original_fetch(sql, *params)

    app.connection.fetch.side_effect = fetch
    await model.read_app_snapshot(
        from_ts=START,
        to_ts=END,
        visibility=UsageVisibility(),
    )

    gap_sql = next(
        sql
        for _operation, sql, _params in app.connection.calls
        if "infra-read:storage-gaps" in sql
    )
    assert "storage_asset_coverage_gaps" in gap_sql


@pytest.mark.asyncio
async def test_live_storage_tails_use_and_recheck_exact_source_policy():
    control = {
        "cutover_state": "active",
        "cutover_at": START,
        "last_closed_day": None,
    }
    app = _AppPool(control)
    app.connection.fetch = AsyncMock(side_effect=app.connection.fetch)
    policy = _storage_policy(("claim-requested", "kubernetes-pods", "main-dev"))
    model = SourceAwareUsageReadModel(
        _AuditPool(),
        app,
        enabled_resources=("workspace_pod", "workspace_pvc"),
        storage_publication_policy=policy,
    )
    unauthorized = _interval(
        inventory_scope_id=SCOPE_ID,
        source_kind="pvc",
        resource="workspace_pvc",
        measurement_basis="claim-requested",
        category="storage",
        resource_class="persistent-volume-claim",
        cpu_millicores=None,
        memory_bytes=None,
        storage_bytes=4 * 1024**3,
        inventory_collector_id="kubevirt-storage",
        inventory_source_cluster="main-dev",
        inventory_namespace="srw",
    )
    authorized = {
        **unauthorized,
        "id": UUID("10000000-0000-0000-0000-000000000011"),
        "inventory_collector_id": "kubernetes-pods",
    }
    original_fetch = app.connection.fetch.side_effect

    async def fetch(sql: str, *params: Any):
        if "infra-read:intervals" in sql:
            app.connection._record("fetch", sql, params)
            return [unauthorized, authorized]
        return await original_fetch(sql, *params)

    app.connection.fetch.side_effect = fetch
    snapshot = await model.read_app_snapshot(
        from_ts=START,
        to_ts=END,
        visibility=UsageVisibility(),
    )

    assert [row["id"] for row in snapshot.intervals] == [authorized["id"]]
    assert model._storage_tail_row_is_authorized(
        {
            "source_measurement_basis": "claim-requested",
            "inventory_collector_id": "kubernetes-pods",
            "inventory_source_cluster": "main-dev",
            "interval_source_cluster": "main-dev",
        },
        basis_field="source_measurement_basis",
    )
    assert not model._storage_tail_row_is_authorized(
        {
            "source_measurement_basis": "claim-requested",
            "inventory_collector_id": "kubevirt-storage",
            "inventory_source_cluster": "main-dev",
            "interval_source_cluster": "main-dev",
        },
        basis_field="source_measurement_basis",
    )
    assert not model._storage_tail_row_is_authorized(
        {
            "source_measurement_basis": "claim-requested",
            "inventory_collector_id": "kubernetes-pods",
            "inventory_source_cluster": "vm-cluster",
            "interval_source_cluster": "vm-cluster",
        },
        basis_field="source_measurement_basis",
    )
    for marker in (
        "infra-read:intervals",
        "infra-read:plan-headers",
        "infra-read:correction-plan-headers",
    ):
        _operation, sql, params = next(
            call for call in app.connection.calls if marker in call[1]
        )
        assert "unnest($4::text[], $5::text[], $6::text[])" in sql
        assert params[3:6] == (
            ["claim-requested"],
            ["kubernetes-pods"],
            ["main-dev"],
        )


@pytest.mark.asyncio
async def test_required_storage_scope_fails_closed_until_resource_is_enabled():
    snapshot = _snapshot(
        epochs=(_epoch(api_resource="core/v1/persistentvolumeclaims"),),
    )
    audit = _AuditPool()
    model = SourceAwareUsageReadModel(audit, _AppPool(None))
    model.read_app_snapshot = AsyncMock(return_value=snapshot)  # type: ignore[method-assign]

    result = await model.summary(
        from_ts=START,
        to_ts=END,
        visibility=UsageVisibility(),
        as_of=END,
    )

    assert result.coverage.status == "partial"
    assert result.coverage.required_sources_ok == 0
    assert result.coverage.required_sources_total == 1
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": START, "end": END}
    ]


@pytest.mark.asyncio
async def test_identical_storage_api_resources_require_exact_authority():
    other_epoch_id = UUID("70000000-0000-0000-0000-000000000008")
    other_scope_id = UUID("60000000-0000-0000-0000-000000000008")
    policy = _storage_policy(("claim-requested", "kubernetes-pods", "main-dev"))
    snapshot = _snapshot(
        epochs=(
            _epoch(
                api_resource="core/v1/persistentvolumeclaims",
                collector_id="kubernetes-pods",
            ),
            _epoch(
                id=other_epoch_id,
                scope_id=other_scope_id,
                api_resource="core/v1/persistentvolumeclaims",
                collector_id="kubevirt-storage",
            ),
        ),
        storage_requirements=(_storage_requirement(),),
    )

    result, _ = await _summarize(
        snapshot,
        enabled_resources=("workspace_pvc",),
        storage_publication_policy=policy,
    )

    assert result.coverage.status == "partial"
    assert result.coverage.required_sources_ok == 1
    assert result.coverage.required_sources_total == 2
    assert result.window.data_through == START
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": START, "end": END}
    ]


@pytest.mark.asyncio
async def test_missing_policy_required_storage_source_is_never_complete_zero():
    policy = _storage_policy(("claim-requested", "kubevirt-storage", "vm-cluster"))

    result, _ = await _summarize(
        _snapshot(epochs=(_epoch(),)),
        enabled_resources=("workspace_pod", "workspace_pvc"),
        storage_publication_policy=policy,
    )

    assert result.coverage.status == "partial"
    assert result.coverage.required_sources_ok == 1
    assert result.coverage.required_sources_total == 2
    assert result.window.data_through == START
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": START, "end": END}
    ]


@pytest.mark.asyncio
async def test_storage_epoch_boundary_replacement_preserves_complete_window():
    boundary = START + timedelta(minutes=30)
    replacement_epoch_id = UUID("70000000-0000-0000-0000-000000000009")
    policy = _storage_policy(("claim-requested", "kubernetes-pods", "main-dev"))
    snapshot = _snapshot(
        epochs=(
            _epoch(
                api_resource="core/v1/persistentvolumeclaims",
                retired_at=boundary,
                complete_through=boundary,
            ),
            _epoch(
                id=replacement_epoch_id,
                api_resource="core/v1/persistentvolumeclaims",
                required_from=boundary,
                complete_through=END,
            ),
        ),
        storage_requirements=(_storage_requirement(),),
    )

    result, _ = await _summarize(
        snapshot,
        enabled_resources=("workspace_pvc",),
        storage_publication_policy=policy,
    )

    assert result.coverage.status == "complete"
    assert result.coverage.required_sources_ok == 1
    assert result.coverage.required_sources_total == 1
    assert result.window.data_through == END


@pytest.mark.asyncio
async def test_compute_exact_epoch_retirement_cannot_inherit_healthy_successor() -> (
    None
):
    boundary = START + timedelta(minutes=30)
    successor_id = UUID("70000000-0000-0000-0000-00000000000a")
    snapshot = _snapshot(
        epochs=(
            _epoch(retired_at=boundary, complete_through=boundary),
            _epoch(
                id=successor_id,
                required_from=boundary,
                complete_through=END,
            ),
        ),
        compute_requirements=(_compute_requirement(retired_at=boundary),),
    )

    result, _ = await _summarize(
        snapshot,
        enabled_resources=("workspace_pod", "agent_pod"),
    )

    assert result.coverage.status == "partial"
    assert result.coverage.required_sources_ok == 1
    assert result.coverage.required_sources_total == 2
    assert result.window.data_through == boundary
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": boundary, "end": END}
    ]


@pytest.mark.asyncio
async def test_compute_recovery_authority_resumes_without_bridging_gap() -> None:
    retirement = START + timedelta(minutes=25)
    promoted_at = retirement + timedelta(minutes=5)
    successor_id = UUID("70000000-0000-0000-0000-00000000000b")
    snapshot = _snapshot(
        epochs=(
            _epoch(retired_at=retirement, complete_through=retirement),
            _epoch(
                id=successor_id,
                required_from=retirement,
                complete_through=END,
            ),
        ),
        compute_requirements=(
            _compute_requirement(retired_at=retirement),
            _compute_requirement(
                epoch_id=successor_id,
                authority_sequence=2,
                authority_effective_from=promoted_at,
                complete_through=END,
            ),
        ),
    )

    result, _ = await _summarize(
        snapshot,
        enabled_resources=("workspace_pod", "agent_pod"),
    )

    assert result.coverage.status == "partial"
    assert result.coverage.required_sources_ok == 1
    assert result.coverage.required_sources_total == 2
    assert result.window.data_through == retirement
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": retirement, "end": promoted_at}
    ]


@pytest.mark.asyncio
async def test_agent_authority_gap_does_not_degrade_legacy_workspace_coverage() -> None:
    gap_start = START + timedelta(minutes=10)
    gap_end = gap_start + timedelta(minutes=5)
    snapshot = _snapshot(
        epochs=(_epoch(),),
        gaps=(
            {
                "scope_epoch_id": EPOCH_ID,
                "gap_start": gap_start,
                "gap_end": gap_end,
                "resolution": "waived",
                "reason": ("compute-authority-awaiting-confirmation:agent_pod"),
            },
        ),
    )

    result, _ = await _summarize(
        snapshot,
        enabled_resources=("workspace_pod",),
    )

    assert result.coverage.status == "complete"
    assert result.coverage.unknown_ranges == []


@pytest.mark.asyncio
async def test_pv_only_coverage_uses_pvc_as_attribution_not_claim_quantity():
    pv_scope_id = UUID("60000000-0000-0000-0000-000000000020")
    pvc_scope_id = UUID("60000000-0000-0000-0000-000000000021")
    policy = _storage_policy(("volume-provisioned", "kubernetes-pods", "main-dev"))
    snapshot = _snapshot(
        epochs=(
            _epoch(
                id=UUID("70000000-0000-0000-0000-000000000020"),
                scope_id=pv_scope_id,
                api_resource="core/v1/persistentvolumes",
                namespace=None,
            ),
            _epoch(
                id=UUID("70000000-0000-0000-0000-000000000021"),
                scope_id=pvc_scope_id,
                api_resource="core/v1/persistentvolumeclaims",
            ),
        ),
        storage_requirements=(
            _storage_requirement(
                measurement_basis="volume-provisioned",
                inventory_scope_id=pv_scope_id,
            ),
            _storage_requirement(
                measurement_basis="volume-provisioned",
                inventory_scope_id=pvc_scope_id,
                requirement_role="attribution",
            ),
        ),
    )

    result, _ = await _summarize(
        snapshot,
        enabled_resources=("block_volume_local_path",),
        storage_publication_policy=policy,
    )

    assert result.coverage.status == "complete"
    assert result.coverage.required_sources_ok == 2
    assert result.coverage.required_sources_total == 2
    assert result.window.data_through == END


@pytest.mark.asyncio
async def test_pv_only_coverage_fails_closed_without_pvc_attribution_epoch():
    pv_scope_id = UUID("60000000-0000-0000-0000-000000000022")
    pvc_scope_id = UUID("60000000-0000-0000-0000-000000000023")
    policy = _storage_policy(("volume-provisioned", "kubernetes-pods", "main-dev"))
    snapshot = _snapshot(
        epochs=(
            _epoch(
                id=UUID("70000000-0000-0000-0000-000000000022"),
                scope_id=pv_scope_id,
                api_resource="core/v1/persistentvolumes",
                namespace=None,
            ),
        ),
        storage_requirements=(
            _storage_requirement(
                measurement_basis="volume-provisioned",
                inventory_scope_id=pv_scope_id,
            ),
            _storage_requirement(
                measurement_basis="volume-provisioned",
                inventory_scope_id=pvc_scope_id,
                requirement_role="attribution",
            ),
        ),
    )

    result, _ = await _summarize(
        snapshot,
        enabled_resources=("block_volume_local_path",),
        storage_publication_policy=policy,
    )

    assert result.coverage.status == "partial"
    assert result.coverage.required_sources_ok == 1
    assert result.coverage.required_sources_total == 2
    assert result.window.data_through == START
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": START, "end": END}
    ]


@pytest.mark.asyncio
async def test_provisional_cpu_and_ram_integrate_capacity_and_clip_last_confirmed():
    query_end = START + timedelta(hours=2)
    matching_rates = (
        {
            **_rate(
                "81000000-0000-0000-0000-000000000008",
                "vcpu-hour",
                "0.25",
            ),
            "cost_domain": "workload-allocation",
            "measurement_basis": "scheduler-request",
            "category": "compute",
            "resource_class": "kubernetes-pod",
            "resource": "workspace_pod",
        },
        {
            **_rate(
                "82000000-0000-0000-0000-000000000008",
                "gib-hour",
                "0",
            ),
            "cost_domain": "workload-allocation",
            "measurement_basis": "scheduler-request",
            "category": "compute",
            "resource_class": "kubernetes-pod",
            "resource": "workspace_pod",
        },
    )
    snapshot = _snapshot(
        intervals=(_interval(last_confirmed_at=END),),
        rate_rows=matching_rates,
    )

    result, _audit = await _summarize(snapshot, to_ts=query_end)
    rows = {row.unit: row for row in result.rows}

    assert rows["vcpu-hour"].quantity == "8"
    assert rows["gib-hour"].quantity == "16"
    for row in rows.values():
        assert row.finalized_quantity == "0"
        assert row.confirmed_provisional_quantity == row.quantity
        assert row.ledger_cost.status == "unpriced"
        assert row.ledger_cost.amount is None
        assert row.events == 0
    assert result.coverage.includes_provisional is True


@pytest.mark.asyncio
async def test_complete_rolled_days_and_audit_point_tail_are_added_once():
    from_ts = datetime(2026, 8, 5, tzinfo=UTC)
    rolled_end = datetime(2026, 8, 7, tzinfo=UTC)
    to_ts = rolled_end + timedelta(hours=12)
    snapshot = _snapshot(
        cutover_at=from_ts,
        watermark=date(2026, 8, 6),
        rolled_days=(date(2026, 8, 5), date(2026, 8, 6)),
        daily_rows=(_usage_row(unpriced_quantity="8"),),
        epochs=(_epoch(required_from=from_ts, complete_through=to_ts),),
        day_states=(
            _day_state(date(2026, 8, 5)),
            _day_state(date(2026, 8, 6)),
        ),
    )
    audit_tail = _usage_row(
        priced_quantity="4",
        unpriced_quantity="0",
        cost_usd="1",
        priced_events=1,
        unpriced_events=0,
    )

    result, audit = await _summarize(
        snapshot,
        from_ts=from_ts,
        to_ts=to_ts,
        audit_rows=[audit_tail],
    )

    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.quantity == "12"
    assert row.finalized_quantity == "12"
    assert row.confirmed_provisional_quantity == "0"
    assert row.ledger_cost.status == "partially-priced"
    assert row.ledger_cost.amount == "1"
    assert row.ledger_cost.priced_quantity == "4"
    assert row.ledger_cost.unpriced_quantity == "8"
    assert row.events == 2

    sql, params = audit.calls[0]
    assert "event.period_start IS NULL" in sql
    assert "event.source = ANY($7::text[])" in sql
    assert params[2] == rolled_end
    assert params[3:5] == (from_ts, rolled_end)


@pytest.mark.asyncio
async def test_complete_rolled_day_ignores_later_mutable_epoch_health():
    from_ts = datetime(2026, 8, 5, tzinfo=UTC)
    to_ts = from_ts + timedelta(days=1)
    snapshot = _snapshot(
        cutover_at=from_ts,
        watermark=from_ts.date(),
        rolled_days=(from_ts.date(), from_ts.date()),
        epochs=(
            _epoch(
                required_from=from_ts,
                complete_through=to_ts,
                snapshot_health="stale",
                publication_health="initializing",
            ),
        ),
        day_states=(_day_state(from_ts.date()),),
    )

    result, _ = await _summarize(snapshot, from_ts=from_ts, to_ts=to_ts)

    assert result.coverage.status == "complete"
    assert result.coverage.required_sources_total == 0
    assert result.window.data_through == to_ts
    assert result.coverage.unknown_ranges == []


@pytest.mark.asyncio
async def test_missing_rolled_day_state_stops_contiguous_data_through():
    from_ts = datetime(2026, 8, 5, tzinfo=UTC)
    missing_start = from_ts + timedelta(days=1)
    to_ts = missing_start + timedelta(days=1)
    snapshot = _snapshot(
        cutover_at=from_ts,
        watermark=(to_ts - timedelta(days=1)).date(),
        rolled_days=(from_ts.date(), missing_start.date()),
        day_states=(_day_state(from_ts.date()),),
    )

    result, _ = await _summarize(snapshot, from_ts=from_ts, to_ts=to_ts)

    assert result.coverage.status == "partial"
    assert result.window.data_through == missing_start
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": missing_start, "end": to_ts}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    (
        _day_state(
            START.date(),
            rollup_revision="old-coverage-revision",
            current_revision="new-coverage-revision",
        ),
        _day_state(START.date(), current_revision=None),
        _day_state(START.date(), infra_state="sealing"),
    ),
)
async def test_stale_or_unsealed_rolled_coverage_revision_fails_closed(state):
    day_start = datetime(2026, 8, 5, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    snapshot = _snapshot(
        cutover_at=day_start,
        watermark=day_start.date(),
        rolled_days=(day_start.date(), day_start.date()),
        daily_rows=(_usage_row(),),
        day_states=(state,),
    )

    result, _ = await _summarize(
        snapshot,
        from_ts=day_start,
        to_ts=day_end,
    )

    assert result.coverage.status == "partial"
    assert result.window.data_through == day_start
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": day_start, "end": day_end}
    ]


@pytest.mark.asyncio
async def test_partial_rolled_day_stops_at_first_durable_unknown_range():
    from_ts = datetime(2026, 8, 5, tzinfo=UTC)
    to_ts = from_ts + timedelta(days=1)
    gap_start = from_ts + timedelta(hours=12)
    gap_end = gap_start + timedelta(hours=1)
    snapshot = _snapshot(
        cutover_at=from_ts,
        watermark=from_ts.date(),
        rolled_days=(from_ts.date(), from_ts.date()),
        day_states=(
            _day_state(
                from_ts.date(),
                coverage_status="partial",
                unknown_ranges=[
                    {"start": gap_start.isoformat(), "end": gap_end.isoformat()}
                ],
            ),
        ),
    )

    result, _ = await _summarize(snapshot, from_ts=from_ts, to_ts=to_ts)

    assert result.coverage.status == "partial"
    assert result.window.data_through == gap_start
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": gap_start, "end": gap_end}
    ]


@pytest.mark.asyncio
async def test_partial_rolled_day_is_complete_for_query_outside_unknown_range():
    day_start = datetime(2026, 8, 5, tzinfo=UTC)
    gap_start = day_start + timedelta(hours=1)
    gap_end = gap_start + timedelta(hours=1)
    snapshot = _snapshot(
        cutover_at=day_start,
        watermark=day_start.date(),
        day_states=(
            _day_state(
                day_start.date(),
                coverage_status="partial",
                unknown_ranges=[
                    {"start": gap_start.isoformat(), "end": gap_end.isoformat()}
                ],
            ),
        ),
    )

    result, _ = await _summarize(snapshot)

    assert result.coverage.status == "complete"
    assert result.window.data_through == END
    assert result.coverage.unknown_ranges == []


@pytest.mark.asyncio
async def test_live_inventory_coverage_does_not_depend_on_publication_health():
    snapshot = _snapshot(
        epochs=(
            _epoch(
                publication_health="initializing",
            ),
        )
    )

    result, _ = await _summarize(snapshot)

    assert result.coverage.status == "complete"
    assert result.coverage.required_sources_ok == 1
    assert result.coverage.required_sources_total == 1
    assert result.window.data_through == END


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("health_field", "health_value"),
    (
        ("snapshot_health", "stale"),
        ("backend_health", "degraded"),
    ),
)
async def test_freshness_health_freezes_coverage_at_last_complete_proof(
    health_field: str,
    health_value: str,
):
    complete_through = START + timedelta(minutes=30)
    snapshot = _snapshot(
        epochs=(
            _epoch(
                complete_through=complete_through,
                **{health_field: health_value},
            ),
        ),
    )

    result, _ = await _summarize(snapshot)

    assert result.coverage.status == "partial"
    assert result.coverage.required_sources_ok == 0
    assert result.coverage.required_sources_total == 1
    assert result.window.data_through == complete_through
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": complete_through, "end": END}
    ]


@pytest.mark.asyncio
async def test_visibility_and_ref_are_applied_to_app_and_audit_sources():
    app = _AppPool(
        {
            "cutover_state": "active",
            "cutover_at": START,
            "last_closed_day": None,
        }
    )
    audit = _AuditPool()
    model = SourceAwareUsageReadModel(audit, app)
    visible_project = UUID("91000000-0000-0000-0000-000000000009")
    scoped_project = UUID("92000000-0000-0000-0000-000000000009")
    ref_id = UUID("93000000-0000-0000-0000-000000000009")
    visibility = UsageVisibility(
        owner_user_id=str(USER_ID),
        visible_project_ids=(str(visible_project),),
        scope_project_id=str(scoped_project),
    )

    await model.summary(
        from_ts=START,
        to_ts=END,
        visibility=visibility,
        ref_id=str(ref_id),
        as_of=END,
    )

    _operation, interval_sql, interval_params = next(
        call for call in app.connection.calls if "infra-read:intervals" in call[1]
    )
    assert "interval.attribution_scope = 'customer'" in interval_sql
    assert "interval.user_id = $7" in interval_sql
    assert "interval.project_id = ANY($8::uuid[])" in interval_sql
    assert "interval.project_id = $9" in interval_sql
    assert "interval.owner_id = $10" in interval_sql
    assert interval_params[6:] == (
        USER_ID,
        [visible_project],
        scoped_project,
        str(ref_id),
    )

    _operation, correction_sql, correction_params = next(
        call
        for call in app.connection.calls
        if "infra-read:correction-plan-headers" in call[1]
    )
    assert "visible_event.event_payload ->> 'user_id'" in correction_sql
    assert "visible_event.event_payload ->> 'project_id'" in correction_sql
    assert "visible_event.event_payload ->> 'ref_id'" in correction_sql
    assert correction_params[6:] == (
        str(USER_ID),
        [str(visible_project)],
        str(scoped_project),
        str(ref_id),
    )

    audit_sql, audit_params = audit.calls[0]
    assert "event.user_id = $9" in audit_sql
    assert "event.project_id = ANY($10::uuid[])" in audit_sql
    assert "event.project_id = $11" in audit_sql
    assert "event.ref_id = $12" in audit_sql
    assert audit_params[8:] == (
        USER_ID,
        [visible_project],
        scoped_project,
        ref_id,
    )


@pytest.mark.asyncio
async def test_planned_crash_window_stays_provisional_until_cursor_is_finalized():
    source = _interval(ended_at=END, end_reason="deleted")
    plan = build_usage_plan(source, (), creator_generation=1)
    assert plan is not None
    planned_snapshot = _snapshot(intervals=(source,), plans=(plan,))
    published_interval = {**source, "materialized_through": END}
    published_snapshot = _snapshot(
        intervals=(published_interval,),
        plans=(replace(plan, state="published"),),
    )

    planned_result, _ = await _summarize(planned_snapshot)
    published_result, _ = await _summarize(published_snapshot)
    planned = {row.unit: row for row in planned_result.rows}
    published = {row.unit: row for row in published_result.rows}

    assert planned["vcpu-hour"].quantity == published["vcpu-hour"].quantity == "8"
    assert planned["gib-hour"].quantity == published["gib-hour"].quantity == "16"
    assert planned["vcpu-hour"].finalized_quantity == "0"
    assert planned["vcpu-hour"].confirmed_provisional_quantity == "8"
    assert planned["vcpu-hour"].events == 0
    assert published["vcpu-hour"].finalized_quantity == "8"
    assert published["vcpu-hour"].confirmed_provisional_quantity == "0"
    assert published["vcpu-hour"].events == 1


@pytest.mark.asyncio
async def test_published_late_usage_advances_the_same_contiguous_interval_cursor():
    source = _interval(ended_at=END, end_reason="late-discovery")
    late_plan = build_late_usage_plan(
        source,
        (),
        creator_generation=1,
        discovered_at=END + timedelta(minutes=1),
        discovery_evidence={"inventory_snapshot": "late-snapshot-1"},
    )
    assert late_plan is not None
    snapshot = _snapshot(
        intervals=({**source, "materialized_through": END},),
        plans=(replace(late_plan, state="published"),),
    )

    result, _ = await _summarize(snapshot)
    rows = {row.unit: row for row in result.rows}

    assert rows["vcpu-hour"].finalized_quantity == "8"
    assert rows["gib-hour"].finalized_quantity == "16"
    assert all(row.confirmed_provisional_quantity == "0" for row in rows.values())


def _published_attribution_correction() -> tuple[
    dict[str, Any], FrozenPublicationPlan, FrozenPublicationPlan
]:
    source = _interval(ended_at=END, end_reason="deleted")
    ordinary = build_usage_plan(source, (), creator_generation=1)
    assert ordinary is not None
    cpu_event = next(
        event for event in ordinary.events if event.event.payload["unit"] == "vcpu-hour"
    )
    correction = build_correction_plan(
        source,
        (
            CorrectionDelta(original=cpu_event, quantity="-8"),
            CorrectionDelta(
                original=cpu_event,
                quantity="4",
                payload_overrides={
                    "user_id": str(NEW_USER_ID),
                    "project_id": str(NEW_PROJECT_ID),
                    "ref_kind": "job",
                    "ref_id": str(NEW_OWNER_ID),
                    "source_capacity_value": "4000",
                },
            ),
        ),
        correction_reason="repair attribution and requested CPU",
        correction_actor_id=USER_ID,
        creator_generation=1,
        plan_revision=1,
        discovered_at=END + timedelta(minutes=1),
    )
    return (
        {**source, "materialized_through": END},
        replace(ordinary, state="published"),
        replace(correction, state="published"),
    )


@pytest.mark.asyncio
async def test_signed_correction_is_a_delta_and_never_doubles_cursor_coverage():
    interval, ordinary, correction = _published_attribution_correction()
    result, _ = await _summarize(
        _snapshot(intervals=(interval,), plans=(ordinary, correction))
    )
    rows = {row.unit: row for row in result.rows}

    # Fleet visibility sees +8 ordinary, -8 reversal, +4 replacement.  The
    # correction plan must not also count as a second finalized cursor range.
    assert rows["vcpu-hour"].finalized_quantity == "4"
    assert rows["vcpu-hour"].events == 3
    assert rows["gib-hour"].finalized_quantity == "16"


@pytest.mark.asyncio
async def test_correction_visibility_is_authoritative_per_verified_payload():
    interval, ordinary, correction = _published_attribution_correction()

    old_result, _ = await _summarize(
        _snapshot(intervals=(interval,), plans=(ordinary, correction)),
        visibility=UsageVisibility(owner_user_id=str(USER_ID)),
    )
    new_result, _ = await _summarize(
        # The replacement owner cannot see the old source interval, but the
        # positive correction payload is independently visible to them.
        _snapshot(plans=(correction,)),
        visibility=UsageVisibility(owner_user_id=str(NEW_USER_ID)),
    )

    old_rows = {row.unit: row for row in old_result.rows}
    new_rows = {row.unit: row for row in new_result.rows}
    assert old_rows["vcpu-hour"].finalized_quantity == "0"
    assert old_rows["gib-hour"].finalized_quantity == "16"
    assert new_rows["vcpu-hour"].finalized_quantity == "4"
    assert "gib-hour" not in new_rows


@pytest.mark.asyncio
async def test_signed_audit_tail_rows_net_with_existing_finalized_usage():
    snapshot = _snapshot(daily_rows=(_usage_row(unpriced_quantity="8"),))
    correction_row = _usage_row(unpriced_quantity="-4")

    result, audit = await _summarize(snapshot, audit_rows=[correction_row])

    assert result.rows[0].finalized_quantity == "4"
    assert "sign(event.quantity)" in audit.calls[0][0]


@pytest.mark.asyncio
async def test_overlapping_published_plans_fail_before_returning_doubled_usage():
    source = _interval(ended_at=END, end_reason="deleted")
    full_plan = build_usage_plan(source, (), creator_generation=1)
    overlap_start = START + timedelta(minutes=30)
    nested_plan = build_usage_plan(
        {**source, "materialized_through": overlap_start},
        (),
        creator_generation=1,
    )
    assert full_plan is not None
    assert nested_plan is not None
    snapshot = _snapshot(
        intervals=({**source, "materialized_through": END},),
        plans=(
            replace(full_plan, state="published"),
            replace(nested_plan, state="published"),
        ),
    )

    with pytest.raises(UsageReadContractError, match="finalized overlap"):
        await _summarize(snapshot)


@pytest.mark.asyncio
async def test_published_free_rate_is_priced_but_missing_rate_is_unpriced():
    rates = (
        _rate(
            "a1000000-0000-0000-0000-00000000000a",
            "vcpu-hour",
            "0.25",
        ),
        _rate(
            "a2000000-0000-0000-0000-00000000000a",
            "gib-hour",
            "0",
        ),
    )
    priced_interval, priced_plan = _published_plan(rates=rates)
    unpriced_interval, unpriced_plan = _published_plan()

    priced_result, _ = await _summarize(
        _snapshot(intervals=(priced_interval,), plans=(priced_plan,))
    )
    unpriced_result, _ = await _summarize(
        _snapshot(intervals=(unpriced_interval,), plans=(unpriced_plan,))
    )
    priced = {row.unit: row for row in priced_result.rows}
    unpriced = {row.unit: row for row in unpriced_result.rows}

    assert priced["vcpu-hour"].ledger_cost.status == "priced"
    assert priced["vcpu-hour"].ledger_cost.amount == "2"
    assert priced["gib-hour"].ledger_cost.status == "priced"
    assert priced["gib-hour"].ledger_cost.amount == "0"
    for row in unpriced.values():
        assert row.ledger_cost.status == "unpriced"
        assert row.ledger_cost.amount is None
        assert row.ledger_cost.priced_quantity == "0"
        assert row.ledger_cost.unpriced_quantity == row.quantity


@pytest.mark.asyncio
async def test_unresolved_coverage_gap_limits_contiguous_data_watermark():
    gap_start = START + timedelta(minutes=20)
    gap_end = START + timedelta(minutes=30)
    snapshot = _snapshot(
        epochs=(
            _epoch(
                complete_through=END,
                continuity_health="gap",
            ),
        ),
        gaps=(
            {
                "scope_epoch_id": EPOCH_ID,
                "gap_start": gap_start,
                "gap_end": gap_end,
                "resolution": "unresolved",
            },
        ),
    )

    result, _ = await _summarize(snapshot)

    assert result.coverage.status == "partial"
    assert result.coverage.required_sources_ok == 0
    assert result.coverage.required_sources_total == 1
    assert result.window.data_through == gap_start
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": gap_start, "end": gap_end}
    ]


@pytest.mark.asyncio
async def test_continuity_gap_without_durable_gap_evidence_fails_closed():
    snapshot = _snapshot(
        epochs=(
            _epoch(
                complete_through=END,
                continuity_health="gap",
            ),
        ),
    )

    result, _ = await _summarize(snapshot)

    assert result.coverage.status == "partial"
    assert result.coverage.required_sources_ok == 0
    assert result.coverage.required_sources_total == 1
    assert result.window.data_through == START
    assert [item.model_dump() for item in result.coverage.unknown_ranges] == [
        {"start": START, "end": END}
    ]
