"""Source-aware usage read model for the gated Slice 1 handoff.

When its independent source-aware-read gate is off, the public v2 route uses the
Slice 0 point-event adapter.  When the gate is on, this reader consumes the
app-owned rollup boundary, daily rows, publication plans, intervals, and
coverage from one read-only repeatable-read snapshot, then combines that
snapshot with the immutable audit tail.

The durable cutover row is a hard safety boundary.  Shadow intervals are never
canonical usage, so this reader refuses to run unless cutover is ``active`` and
clips all interval-derived usage at ``cutover_at``.  Publication remains a
separate gate and this module never writes either database.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping, Protocol, Sequence

import asyncpg

from orchestrator.services.infrastructure_metering.materializer import (
    CapacityDimension,
    FrozenPublicationPlan,
    PublicationContractError,
    StorageCoverageRequirement,
    StoragePublicationAuthority,
    StoragePublicationPolicy,
    capacity_dimensions,
    capacity_quantity,
)
from orchestrator.services.infrastructure_metering.types import (
    UsageCoverageV2,
    UsageRowV2,
    UsageSummaryV2,
    UsageWindowV2,
    decimal_text,
    ledger_cost,
)

_UTC = timezone.utc
_INFRA_SOURCES = (
    "infra-allocation-v2",
    "infra-allocation-correction-v2",
)
_DEFAULT_ENABLED_RESOURCES = ("workspace_pod",)
_RESOURCE_API_RESOURCES = {
    "workspace_pod": frozenset({"core/v1/pods"}),
    "agent_pod": frozenset({"core/v1/pods"}),
    "workspace_vm": frozenset({"kubevirt.io/v1/virtualmachineinstances"}),
    "workspace_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "session_workspace_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "session_agent_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "persistent_agent_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "vm_rootdisk_claim": frozenset({"core/v1/persistentvolumeclaims"}),
    "golden_image_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "platform_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "unclassified_pvc": frozenset({"core/v1/persistentvolumeclaims"}),
    "unmapped_block_volume": frozenset({"core/v1/persistentvolumes"}),
}
_MAPPED_VOLUME_RESOURCE_RE = re.compile(r"^block_volume_[a-z0-9_]+$")
_STORAGE_BASIS_BY_API_RESOURCE = {
    "core/v1/persistentvolumeclaims": "claim-requested",
    "core/v1/persistentvolumes": "volume-provisioned",
}
_COMPUTE_API_RESOURCE = {
    "agent_pod": "core/v1/pods",
    "ide_workspace_pod": "core/v1/pods",
    "workspace_vm": "kubevirt.io/v1/virtualmachineinstances",
}
_COMPUTE_AUTHORITY_GAP_PREFIX = "compute-authority-awaiting-confirmation:"


def _resource_api_resources(resource: str) -> frozenset[str] | None:
    mapped = _RESOURCE_API_RESOURCES.get(resource)
    if mapped is not None:
        return mapped
    if _MAPPED_VOLUME_RESOURCE_RE.fullmatch(resource):
        return frozenset({"core/v1/persistentvolumes"})
    return None


def _storage_authority_from_scope(
    row: Mapping[str, Any],
    *,
    basis_field: str = "measurement_basis",
    measurement_basis: str | None = None,
) -> StoragePublicationAuthority | None:
    basis = str(measurement_basis or row.get(basis_field) or "")
    if basis not in _STORAGE_BASIS_BY_API_RESOURCE.values():
        return None
    collector_id = row.get("inventory_collector_id", row.get("collector_id"))
    source_cluster = row.get(
        "inventory_source_cluster",
        row.get("source_cluster"),
    )
    if not isinstance(collector_id, str) or not isinstance(source_cluster, str):
        return None
    try:
        return StoragePublicationAuthority(
            measurement_basis=basis,
            collector_id=collector_id,
            source_cluster=source_cluster,
        )
    except PublicationContractError:
        return None


def _compute_activation_key_from_interval(row: Mapping[str, Any]) -> str | None:
    if (
        row.get("source_kind") == "pod"
        and row.get("category") == "compute"
        and row.get("resource") == "agent_pod"
    ):
        return "agent_pod"
    details = row.get("details")
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except json.JSONDecodeError:
            details = None
    if (
        row.get("source_kind") == "pod"
        and row.get("category") == "compute"
        and row.get("resource") == "workspace_pod"
        and isinstance(details, Mapping)
        and details.get("product_class") == "ide-session"
    ):
        return "ide_workspace_pod"
    if (
        row.get("source_kind") == "vmi"
        and row.get("category") == "compute"
        and row.get("resource") == "workspace_vm"
    ):
        return "workspace_vm"
    return None


_BASE_EXCLUDED_DOMAINS = (
    "node-assets",
    "idle",
    "network",
    "control-plane",
)


class UsageReadContractError(RuntimeError):
    """App/audit state cannot satisfy the exact read-model contract."""


class UsageReadCutoverInactive(UsageReadContractError):
    """The dark interval handoff was invoked before durable cutover."""


class _Visibility(Protocol):
    owner_user_id: str | None
    visible_project_ids: tuple[str, ...]
    scope_project_id: str | None
    include_non_customer: bool


@dataclass(frozen=True, slots=True)
class UsageDimensions:
    category: str
    measurement_basis: str
    cost_domain: str
    resource_class: str
    measurement_algorithm: str
    resource: str
    unit: str
    attribution_scope: str

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> UsageDimensions:
        values: dict[str, str] = {}
        for field_name in (
            "category",
            "measurement_basis",
            "cost_domain",
            "resource_class",
            "measurement_algorithm",
            "resource",
            "unit",
            "attribution_scope",
        ):
            value = row.get(field_name)
            if not isinstance(value, str) or not value:
                raise UsageReadContractError(
                    f"usage dimension {field_name} must be non-empty text"
                )
            values[field_name] = value
        return cls(**values)


@dataclass(slots=True)
class _UsageTotals:
    finalized_priced: Decimal = Decimal(0)
    finalized_unpriced: Decimal = Decimal(0)
    provisional: Decimal = Decimal(0)
    cost_usd: Decimal = Decimal(0)
    priced_events: int = 0
    unpriced_events: int = 0

    @property
    def events(self) -> int:
        return self.priced_events + self.unpriced_events

    def add_finalized(
        self,
        quantity: Decimal,
        *,
        cost_usd: Decimal | None,
        events: int,
    ) -> None:
        if events < 0:
            raise UsageReadContractError("usage event count cannot be negative")
        if cost_usd is None:
            self.finalized_unpriced = _add(self.finalized_unpriced, quantity)
            self.unpriced_events += events
        else:
            self.finalized_priced = _add(self.finalized_priced, quantity)
            self.cost_usd = _add(self.cost_usd, cost_usd)
            self.priced_events += events

    def add_provisional(self, quantity: Decimal) -> None:
        self.provisional = _add(self.provisional, quantity)


@dataclass(frozen=True, slots=True)
class AppUsageReadSnapshot:
    cutover_at: datetime
    watermark: date | None
    rolled_days: tuple[date, date] | None
    daily_rows: tuple[Mapping[str, Any], ...]
    intervals: tuple[Mapping[str, Any], ...]
    plans: tuple[FrozenPublicationPlan, ...]
    rate_rows: tuple[Mapping[str, Any], ...]
    epochs: tuple[Mapping[str, Any], ...]
    storage_requirements: tuple[Mapping[str, Any], ...]
    gaps: tuple[Mapping[str, Any], ...]
    day_states: tuple[Mapping[str, Any], ...]
    compute_requirements: tuple[Mapping[str, Any], ...] = ()

    @property
    def rolled_cutoff(self) -> datetime | None:
        if self.watermark is None:
            return None
        return _midnight(self.watermark + timedelta(days=1))


@dataclass(frozen=True, slots=True)
class _CoverageResult:
    status: str
    data_through: datetime | None
    required_sources_ok: int
    required_sources_total: int
    unknown_ranges: tuple[tuple[datetime, datetime | None], ...]


_CONTROL_SQL = """
/* infra-read:control */
SELECT control.cutover_state, control.cutover_at, rollup.last_closed_day
FROM infra_metering_control AS control
LEFT JOIN rollup_state AS rollup ON rollup.name = 'usage_daily_v2'
WHERE control.singleton = TRUE
"""

_DAILY_SQL = """
/* infra-read:daily */
SELECT
    category, resource, unit, measurement_basis, resource_class,
    attribution_scope, cost_domain, measurement_algorithm,
    SUM(priced_quantity) AS priced_quantity,
    SUM(unpriced_quantity) AS unpriced_quantity,
    SUM(cost_usd) AS cost_usd,
    SUM(priced_events) AS priced_events,
    SUM(unpriced_events) AS unpriced_events,
    SUM(events) AS events
FROM usage_daily_v2 AS usage
WHERE usage.day >= $1 AND usage.day <= $2
  {visibility}
GROUP BY category, resource, unit, measurement_basis, resource_class,
         attribution_scope, cost_domain, measurement_algorithm
ORDER BY category, measurement_basis, resource_class, resource, unit,
         attribution_scope, cost_domain, measurement_algorithm
"""

_INTERVALS_SQL = """
/* infra-read:intervals */
SELECT interval.*,
       source_scope.collector_id AS inventory_collector_id,
       source_scope.source_cluster AS inventory_source_cluster,
       source_scope.namespace AS inventory_namespace
FROM resource_intervals AS interval
LEFT JOIN resource_inventory_scopes AS source_scope
  ON source_scope.id = interval.inventory_scope_id
WHERE interval.resource = ANY($3::text[])
  {product_class}
  AND (
      interval.measurement_basis NOT IN (
          'claim-requested', 'volume-provisioned'
      )
      OR EXISTS (
          SELECT 1
          FROM unnest($4::text[], $5::text[], $6::text[])
               AS storage_policy(
                   measurement_basis, collector_id, source_cluster
               )
          WHERE storage_policy.measurement_basis = interval.measurement_basis
            AND storage_policy.collector_id = source_scope.collector_id
            AND storage_policy.source_cluster = source_scope.source_cluster
            AND source_scope.source_cluster = interval.source_cluster
      )
  )
  AND tstzrange(interval.started_at, interval.ended_at, '[)')
      && tstzrange($1, $2, '[)')
  AND COALESCE(interval.ended_at, interval.last_confirmed_at) > $1
  {visibility}
ORDER BY interval.started_at, interval.id
"""

_PLAN_HEADERS_SQL = """
/* infra-read:plan-headers */
SELECT plan.*,
       interval.measurement_basis AS source_measurement_basis,
       interval.source_cluster AS interval_source_cluster,
       source_scope.collector_id AS inventory_collector_id,
       source_scope.source_cluster AS inventory_source_cluster,
       source_scope.namespace AS inventory_namespace
FROM resource_publication_plans AS plan
JOIN resource_intervals AS interval ON interval.id = plan.source_interval_id
LEFT JOIN resource_inventory_scopes AS source_scope
  ON source_scope.id = interval.inventory_scope_id
WHERE interval.resource = ANY($3::text[])
  {product_class}
  AND (
      interval.measurement_basis NOT IN (
          'claim-requested', 'volume-provisioned'
      )
      OR EXISTS (
          SELECT 1
          FROM unnest($4::text[], $5::text[], $6::text[])
               AS storage_policy(
                   measurement_basis, collector_id, source_cluster
               )
          WHERE storage_policy.measurement_basis = interval.measurement_basis
            AND storage_policy.collector_id = source_scope.collector_id
            AND storage_policy.source_cluster = source_scope.source_cluster
            AND source_scope.source_cluster = interval.source_cluster
      )
  )
  AND plan.plan_kind IN ('usage', 'late-usage')
  AND tstzrange(plan.period_start, plan.period_end, '[)')
      && tstzrange($1, $2, '[)')
  {visibility}
ORDER BY plan.period_start, plan.id
"""

_CORRECTION_PLAN_HEADERS_SQL = """
/* infra-read:correction-plan-headers */
SELECT plan.*,
       interval.measurement_basis AS source_measurement_basis,
       interval.source_cluster AS interval_source_cluster,
       source_scope.collector_id AS inventory_collector_id,
       source_scope.source_cluster AS inventory_source_cluster,
       source_scope.namespace AS inventory_namespace
FROM resource_publication_plans AS plan
JOIN resource_intervals AS interval ON interval.id = plan.source_interval_id
LEFT JOIN resource_inventory_scopes AS source_scope
  ON source_scope.id = interval.inventory_scope_id
WHERE interval.resource = ANY($3::text[])
  {product_class}
  AND (
      interval.measurement_basis NOT IN (
          'claim-requested', 'volume-provisioned'
      )
      OR EXISTS (
          SELECT 1
          FROM unnest($4::text[], $5::text[], $6::text[])
               AS storage_policy(
                   measurement_basis, collector_id, source_cluster
               )
          WHERE storage_policy.measurement_basis = interval.measurement_basis
            AND storage_policy.collector_id = source_scope.collector_id
            AND storage_policy.source_cluster = source_scope.source_cluster
            AND source_scope.source_cluster = interval.source_cluster
      )
  )
  AND plan.plan_kind = 'correction'
  AND tstzrange(plan.period_start, plan.period_end, '[)')
      && tstzrange($1, $2, '[)')
  AND EXISTS (
      SELECT 1
      FROM resource_publication_plan_events AS visible_event
      WHERE visible_event.plan_id = plan.id
        {visibility}
  )
ORDER BY plan.period_start, plan.id
"""

_PLAN_EVENTS_SQL = """
/* infra-read:plan-events */
SELECT event.plan_id, event.ordinal, event.canonical_rate_version_id,
       event.row_hash, event.event_payload
FROM resource_publication_plan_events AS event
WHERE event.plan_id = ANY($1::uuid[])
ORDER BY event.plan_id, event.ordinal
"""

_RATES_SQL = """
/* infra-read:rates */
SELECT cost_domain, measurement_basis, category, resource_class, resource,
       unit, effective_from, effective_to
FROM usage_rates_v2
WHERE effective_from < $2
  AND (effective_to IS NULL OR effective_to > $1)
ORDER BY effective_from
"""

_EPOCHS_SQL = """
/* infra-read:epochs */
SELECT epoch.id, epoch.scope_id, epoch.required_from, epoch.retired_at,
       epoch.complete_through, epoch.snapshot_health,
       epoch.continuity_health, epoch.item_health, epoch.backend_health,
       epoch.publication_health, scope.api_resource, scope.collector_id,
       scope.source_cluster, scope.namespace
FROM resource_inventory_scope_epochs AS epoch
JOIN resource_inventory_scopes AS scope ON scope.id = epoch.scope_id
WHERE epoch.required_for_rollup = TRUE
  AND epoch.required_from < $2
  AND (epoch.retired_at IS NULL OR epoch.retired_at > $1)
ORDER BY epoch.scope_id, epoch.required_from, epoch.epoch_number
"""

_COMPUTE_REQUIREMENTS_SQL = """
/* infra-read:compute-requirements */
SELECT activation.activation_key, activation.activated_at,
       requirement.inventory_scope_id,
       authority.inventory_scope_epoch_id, authority.authority_sequence,
       authority.effective_from AS authority_effective_from,
       epoch.retired_at, epoch.complete_through, epoch.snapshot_health,
       epoch.continuity_health, epoch.item_health, epoch.backend_health,
       scope.api_resource, scope.collector_id, scope.source_cluster,
       scope.namespace
FROM unnest($1::text[]) AS enabled(activation_key)
JOIN compute_metering_activation AS activation
  ON activation.activation_key = enabled.activation_key
LEFT JOIN compute_metering_scope_requirements AS requirement
  ON requirement.activation_key = activation.activation_key
LEFT JOIN compute_metering_epoch_authorities AS authority
  ON authority.activation_key = requirement.activation_key
 AND authority.inventory_scope_id = requirement.inventory_scope_id
LEFT JOIN resource_inventory_scope_epochs AS epoch
  ON epoch.id = authority.inventory_scope_epoch_id
 AND epoch.scope_id = authority.inventory_scope_id
LEFT JOIN resource_inventory_scopes AS scope
  ON scope.id = requirement.inventory_scope_id
WHERE activation.state = 'active'
  AND activation.activated_at IS NOT NULL
  AND activation.activated_at < $2
  AND statement_timestamp() >= activation.activated_at
ORDER BY activation.activation_key, requirement.inventory_scope_id,
         authority.authority_sequence
"""

_STORAGE_REQUIREMENTS_SQL = """
/* infra-read:storage-requirements */
SELECT requirement.measurement_basis, requirement.collector_id,
       requirement.source_cluster, requirement.inventory_scope_id,
       requirement.requirement_role,
       GREATEST(
           source_activation.activated_at,
           global_activation.activated_at
       ) AS effective_from
FROM storage_metering_source_requirements AS requirement
JOIN storage_metering_source_activations AS source_activation
  ON source_activation.measurement_basis = requirement.measurement_basis
 AND source_activation.collector_id = requirement.collector_id
 AND source_activation.source_cluster = requirement.source_cluster
JOIN storage_metering_activation AS global_activation
  ON global_activation.measurement_basis = requirement.measurement_basis
JOIN unnest($1::text[], $2::text[], $3::text[])
     AS storage_policy(measurement_basis, collector_id, source_cluster)
  ON storage_policy.measurement_basis = requirement.measurement_basis
 AND storage_policy.collector_id = requirement.collector_id
 AND storage_policy.source_cluster = requirement.source_cluster
WHERE source_activation.state = 'active'
  AND source_activation.activated_at IS NOT NULL
  AND global_activation.state = 'active'
  AND global_activation.activated_at IS NOT NULL
  AND statement_timestamp() >= GREATEST(
      source_activation.activated_at,
      global_activation.activated_at
  )
ORDER BY requirement.measurement_basis, requirement.collector_id,
         requirement.source_cluster, requirement.requirement_role,
         requirement.inventory_scope_id
"""

_GAPS_SQL = """
/* infra-read:gaps */
SELECT gap.scope_epoch_id, gap.gap_start, gap.gap_end, gap.resolution,
       gap.reason
FROM resource_inventory_coverage_gaps AS gap
WHERE gap.scope_epoch_id = ANY($1::uuid[])
  AND gap.resolution <> 'backfilled'
  AND gap.gap_start < $3
  AND (gap.gap_end IS NULL OR gap.gap_end > $2)
ORDER BY gap.gap_start, gap.id
"""

_STORAGE_GAPS_SQL = """
/* infra-read:storage-gaps */
SELECT gap.scope_epoch_id, gap.gap_start, gap.gap_end, gap.resolution,
       gap.reason
FROM resource_inventory_coverage_gaps AS gap
WHERE gap.scope_epoch_id = ANY($1::uuid[])
  AND gap.resolution <> 'backfilled'
  AND gap.gap_start < $3
  AND (gap.gap_end IS NULL OR gap.gap_end > $2)

UNION ALL

SELECT gap.scope_epoch_id, gap.gap_start, gap.gap_end,
       CASE WHEN gap.resolution = 'unresolved' THEN 'unresolved'
            ELSE 'waived' END AS resolution,
       NULL::text AS reason
FROM storage_asset_coverage_gaps AS gap
WHERE gap.scope_epoch_id = ANY($1::uuid[])
  AND gap.gap_start < $3
  AND (gap.gap_end IS NULL OR gap.gap_end > $2)
ORDER BY gap_start
"""

_DAY_STATES_SQL = """
/* infra-read:day-states */
SELECT rollup.day, rollup.coverage_status, rollup.unknown_ranges,
       rollup.infra_coverage_revision,
       infra.state AS infra_state,
       infra.coverage_revision AS current_infra_coverage_revision
FROM usage_rollup_day_state AS rollup
LEFT JOIN infra_usage_day_state AS infra ON infra.day = rollup.day
WHERE rollup.day >= $1 AND rollup.day <= $2
ORDER BY rollup.day
"""

_BASIS_SQL = """
COALESCE(event.measurement_basis, CASE
    WHEN event.category = 'llm' THEN 'api-consumed'
    WHEN event.category = 'compute' AND event.resource = 'workspace_pod'
        THEN 'scheduler-request'
    ELSE 'legacy-unknown'
END)
"""

_DOMAIN_SQL = """
COALESCE(event.cost_domain, CASE
    WHEN event.category = 'llm' THEN 'external-service'
    WHEN event.category = 'compute' AND event.resource = 'workspace_pod'
        THEN 'workload-allocation'
    ELSE 'unknown'
END)
"""

_CLASS_SQL = """
COALESCE(event.resource_class, CASE
    WHEN event.category = 'llm' THEN 'llm-model'
    WHEN event.category = 'compute' AND event.resource = 'workspace_pod'
        THEN 'kubernetes-pod'
    ELSE 'unknown'
END)
"""

_ATTRIBUTION_SQL = """
COALESCE(event.attribution_scope, CASE
    WHEN event.user_id IS NOT NULL OR event.project_id IS NOT NULL
        THEN 'customer'
    ELSE 'unknown'
END)
"""

_ALGORITHM_SQL = """
COALESCE(event.measurement_algorithm, CASE
    WHEN event.category = 'llm' THEN 'legacy-point-v1'
    WHEN event.category = 'compute' AND event.resource = 'workspace_pod'
        THEN 'legacy-end-stamped-v1'
    ELSE 'legacy-unknown-v1'
END)
"""

_AUDIT_SQL = f"""
/* infra-read:audit-tail */
WITH selected AS (
    SELECT
        event.category,
        event.resource,
        event.unit,
        {_BASIS_SQL} AS measurement_basis,
        {_DOMAIN_SQL} AS cost_domain,
        {_CLASS_SQL} AS resource_class,
        {_ATTRIBUTION_SQL} AS attribution_scope,
        {_ALGORITHM_SQL} AS measurement_algorithm,
        event.quantity AS window_quantity,
        event.cost_usd AS window_cost,
        event.cost_usd IS NOT NULL AS is_priced,
        FALSE AS is_infrastructure
    FROM usage_events AS event
    WHERE event.period_start IS NULL
      AND event.ts >= $1 AND event.ts < $2
      AND (
          $4::timestamptz IS NULL
          OR event.ts < $4
          OR event.ts >= $5
      )
      {{visibility}}

    UNION ALL

    SELECT
        event.category,
        event.resource,
        event.unit,
        event.measurement_basis,
        event.cost_domain,
        event.resource_class,
        event.attribution_scope,
        event.measurement_algorithm,
        clipped.window_quantity,
        CASE
            WHEN event.rate_usd IS NULL THEN NULL
            WHEN overlap.overlap_start = event.period_start
             AND overlap.overlap_end = event.period_end
                THEN event.cost_usd
            ELSE round_half_even_v2(
                clipped.window_quantity * event.rate_usd,
                18
            )
        END AS window_cost,
        event.rate_usd IS NOT NULL AS is_priced,
        TRUE AS is_infrastructure
    FROM usage_events AS event
    CROSS JOIN LATERAL (
        SELECT GREATEST(event.period_start, $1) AS overlap_start,
               LEAST(event.period_end, $2) AS overlap_end
    ) AS overlap
    CROSS JOIN LATERAL (
        SELECT CASE
            WHEN overlap.overlap_start = event.period_start
             AND overlap.overlap_end = event.period_end
                THEN event.quantity
            ELSE round_half_even_v2(
                sign(event.quantity)
                * event.source_capacity_value
                * (
                    EXTRACT(EPOCH FROM (
                        overlap.overlap_end - overlap.overlap_start
                    )) * 1000000::numeric
                )
                / CASE event.source_capacity_unit
                    WHEN 'millicore' THEN 3600000000000::numeric
                    WHEN 'byte' THEN 3865470566400000000::numeric
                    WHEN 'instance' THEN 3600000000::numeric
                    ELSE NULL
                  END,
                18
            )
        END AS window_quantity
    ) AS clipped
    WHERE event.source = ANY($7::text[])
      AND event.resource = ANY($8::text[])
      AND event.period_start < $2
      AND event.period_end > $1
      AND $3::timestamptz IS NOT NULL
      AND event.period_end <= $3
      AND (
          $4::timestamptz IS NULL
          OR event.period_start < $4
          OR event.period_end > $5
      )
      {{visibility}}
), grouped AS (
    SELECT
        category, resource, unit, measurement_basis, cost_domain,
        resource_class, attribution_scope, measurement_algorithm,
        SUM(window_quantity) FILTER (WHERE is_priced) AS priced_quantity,
        SUM(window_quantity) FILTER (WHERE NOT is_priced) AS unpriced_quantity,
        SUM(window_cost) AS cost_usd,
        COUNT(*) FILTER (WHERE is_priced) AS priced_events,
        COUNT(*) FILTER (WHERE NOT is_priced) AS unpriced_events,
        BOOL_OR(is_infrastructure) AS has_infrastructure
    FROM selected
    WHERE ($6::boolean OR attribution_scope = 'customer')
    GROUP BY category, resource, unit, measurement_basis, cost_domain,
             resource_class, attribution_scope, measurement_algorithm
)
SELECT *, priced_events + unpriced_events AS events
FROM grouped
ORDER BY category, measurement_basis, resource_class, resource, unit,
         attribution_scope, cost_domain, measurement_algorithm
"""


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise UsageReadContractError(f"{field_name} must be timezone-aware")
    return value.astimezone(_UTC)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=_UTC)


def _whole_rolled_days(
    from_ts: datetime,
    to_ts: datetime,
    watermark: date | None,
    *,
    ref_id: str | None,
) -> tuple[date, date] | None:
    """Return inclusive whole days safe to serve from ``usage_daily_v2``."""

    if watermark is None or ref_id is not None:
        return None
    first_day = from_ts.date()
    first_full = (
        first_day if from_ts == _midnight(first_day) else first_day + timedelta(days=1)
    )
    last_full = to_ts.date() - timedelta(days=1)
    high = min(last_full, watermark)
    return None if high < first_full else (first_full, high)


def _as_uuid(value: str, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise UsageReadContractError(f"{field_name} must be a UUID") from exc


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise UsageReadContractError(f"{field_name} must be an exact decimal")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise UsageReadContractError(f"{field_name} must be an exact decimal") from exc
    if not number.is_finite():
        raise UsageReadContractError(f"{field_name} must be finite")
    return Decimal(0) if number.is_zero() else number


def _add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        value = left + right
    return Decimal(0) if value.is_zero() else value


def _multiply(left: Decimal, right: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = 80
        value = left * right
    return Decimal(decimal_text(value))


def _append_visibility(
    clauses: list[str],
    params: list[Any],
    visibility: _Visibility,
    *,
    alias: str,
    ref_id: str | None,
    ref_column: str | None,
    apply_attribution_filter: bool = True,
    text_ref: bool = False,
) -> None:
    if apply_attribution_filter and not visibility.include_non_customer:
        clauses.append(f"{alias}.attribution_scope = 'customer'")
    if visibility.owner_user_id is not None:
        params.append(_as_uuid(visibility.owner_user_id, "owner_user_id"))
        own = f"{alias}.user_id = ${len(params)}"
        projects = [
            _as_uuid(project_id, "visible_project_id")
            for project_id in visibility.visible_project_ids
        ]
        if projects:
            params.append(projects)
            clauses.append(
                f"({own} OR {alias}.project_id = ANY(${len(params)}::uuid[]))"
            )
        else:
            clauses.append(own)
    if visibility.scope_project_id is not None:
        params.append(_as_uuid(visibility.scope_project_id, "scope_project_id"))
        clauses.append(f"{alias}.project_id = ${len(params)}")
    if ref_id is not None:
        if ref_column is None:
            raise UsageReadContractError("this read-model source has no ref dimension")
        parsed_ref = _as_uuid(ref_id, "ref_id")
        params.append(str(parsed_ref) if text_ref else parsed_ref)
        clauses.append(f"{ref_column} = ${len(params)}")


def _append_payload_visibility(
    clauses: list[str],
    params: list[Any],
    visibility: _Visibility,
    *,
    alias: str,
    ref_id: str | None,
) -> None:
    """Apply identity visibility to a frozen plan-event JSON payload.

    Correction attribution may intentionally differ from its source interval.
    The SQL prefilter selects only plans with at least one visible event; the
    complete manifest is still loaded and verified before Python filters each
    event independently.
    """

    payload = f"{alias}.event_payload"
    if not visibility.include_non_customer:
        clauses.append(f"({payload} ->> 'attribution_scope') = 'customer'")
    if visibility.owner_user_id is not None:
        owner = str(_as_uuid(visibility.owner_user_id, "owner_user_id"))
        params.append(owner)
        own = f"({payload} ->> 'user_id') = ${len(params)}"
        projects = [
            str(_as_uuid(project_id, "visible_project_id"))
            for project_id in visibility.visible_project_ids
        ]
        if projects:
            params.append(projects)
            clauses.append(
                f"({own} OR ({payload} ->> 'project_id') = ANY(${len(params)}::text[]))"
            )
        else:
            clauses.append(own)
    if visibility.scope_project_id is not None:
        params.append(str(_as_uuid(visibility.scope_project_id, "scope_project_id")))
        clauses.append(f"({payload} ->> 'project_id') = ${len(params)}")
    if ref_id is not None:
        params.append(str(_as_uuid(ref_id, "ref_id")))
        clauses.append(f"({payload} ->> 'ref_id') = ${len(params)}")


def _visibility_fragment(clauses: Sequence[str]) -> str:
    if not clauses:
        return ""
    return "AND " + "\n  AND ".join(clauses)


def _payload_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise UsageReadContractError(f"{field_name} must be canonical timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UsageReadContractError(f"{field_name} is not a timestamp") from exc
    return _aware_utc(parsed, field_name)


def _payload_uuid(payload: Mapping[str, Any], field_name: str) -> uuid.UUID | None:
    value = payload.get(field_name)
    if value is None:
        return None
    return _as_uuid(str(value), f"payload.{field_name}")


def _payload_is_visible(
    payload: Mapping[str, Any],
    visibility: _Visibility,
    *,
    ref_id: str | None,
) -> bool:
    """Mirror the correction-plan SQL prefilter for one verified event."""

    attribution_scope = payload.get("attribution_scope")
    if not isinstance(attribution_scope, str) or not attribution_scope:
        raise UsageReadContractError(
            "publication payload attribution_scope must be non-empty text"
        )
    if not visibility.include_non_customer and attribution_scope != "customer":
        return False

    user_id = _payload_uuid(payload, "user_id")
    project_id = _payload_uuid(payload, "project_id")
    if visibility.owner_user_id is not None:
        owner_id = _as_uuid(visibility.owner_user_id, "owner_user_id")
        projects = {
            _as_uuid(value, "visible_project_id")
            for value in visibility.visible_project_ids
        }
        if user_id != owner_id and project_id not in projects:
            return False
    if visibility.scope_project_id is not None and project_id != _as_uuid(
        visibility.scope_project_id, "scope_project_id"
    ):
        return False
    if ref_id is not None and _payload_uuid(payload, "ref_id") != _as_uuid(
        ref_id, "ref_id"
    ):
        return False
    return True


def _payload_capacity(payload: Mapping[str, Any]) -> CapacityDimension:
    try:
        raw_capacity = payload["source_capacity_value"]
        capacity = int(str(raw_capacity))
    except (KeyError, TypeError, ValueError) as exc:
        raise UsageReadContractError(
            "publication source capacity must be an integer"
        ) from exc
    if (
        capacity < 0
        or isinstance(raw_capacity, bool)
        or str(capacity) != str(raw_capacity)
    ):
        raise UsageReadContractError(
            "publication source capacity must be canonical/nonnegative"
        )

    unit = str(payload.get("unit"))
    capacity_unit = str(payload.get("source_capacity_unit"))
    if unit == "vcpu-hour" and capacity_unit == "millicore":
        denominator = 1000 * 3_600_000_000
    elif unit == "gib-hour" and capacity_unit == "byte":
        denominator = 1024**3 * 3_600_000_000
    elif unit in {"claim-hour", "volume-hour"} and capacity_unit == "instance":
        denominator = 3_600_000_000
    else:
        raise UsageReadContractError(
            "publication unit/source-capacity pair is unsupported"
        )
    return CapacityDimension(unit, capacity, capacity_unit, denominator)


def _interval_dimensions(
    interval: Mapping[str, Any],
) -> dict[str, tuple[UsageDimensions, CapacityDimension]]:
    base = {
        "category": str(interval["category"]),
        "measurement_basis": str(interval["measurement_basis"]),
        "cost_domain": str(interval["cost_domain"]),
        "resource_class": str(interval["resource_class"]),
        "measurement_algorithm": str(interval["measurement_algorithm"]),
        "resource": str(interval["resource"]),
        "attribution_scope": str(interval["attribution_scope"]),
    }
    try:
        capacities = capacity_dimensions(interval)
    except PublicationContractError as exc:
        raise UsageReadContractError("interval capacity shape is invalid") from exc
    return {
        capacity.unit: (
            UsageDimensions.from_mapping({**base, "unit": capacity.unit}),
            capacity,
        )
        for capacity in capacities
    }


def _validate_payload_attribution(
    payload: Mapping[str, Any],
    interval: Mapping[str, Any],
) -> None:
    """Keep app-side visibility identical to the frozen audit attribution."""

    scope = str(interval["attribution_scope"])
    customer = scope == "customer"
    expected = {
        "user_id": (
            str(_as_uuid(str(interval["user_id"]), "interval.user_id"))
            if customer
            else None
        ),
        "project_id": (
            None
            if not customer or interval.get("project_id") is None
            else str(_as_uuid(str(interval["project_id"]), "interval.project_id"))
        ),
        "ref_kind": str(interval["owner_kind"]) if customer else None,
        "ref_id": (
            str(_as_uuid(str(interval["owner_id"]), "interval.owner_id"))
            if customer
            else None
        ),
        "source_cluster": str(interval["source_cluster"]),
        "source_kind": str(interval["source_kind"]),
        "source_uid": str(interval["source_uid"]),
        "source_lifecycle_id": str(
            _as_uuid(
                str(interval["source_lifecycle_id"]),
                "interval.source_lifecycle_id",
            )
        ),
        "source_interval_id": str(_as_uuid(str(interval["id"]), "interval.id")),
    }
    for field_name, expected_value in expected.items():
        actual = payload.get(field_name)
        if actual != expected_value:
            raise UsageReadContractError(
                f"publication payload {field_name} differs from its interval"
            )


def _matching_rate_boundaries(
    interval: Mapping[str, Any],
    dimensions: Mapping[str, tuple[UsageDimensions, CapacityDimension]],
    rate_rows: Sequence[Mapping[str, Any]],
    start: datetime,
    end: datetime,
) -> set[datetime]:
    boundaries: set[datetime] = set()
    selectors = (
        "cost_domain",
        "measurement_basis",
        "category",
        "resource_class",
        "resource",
    )
    for rate in rate_rows:
        if str(rate["unit"]) not in dimensions or any(
            str(rate[field]) != str(interval[field]) for field in selectors
        ):
            continue
        effective_from = _aware_utc(rate["effective_from"], "effective_from")
        effective_to = (
            None
            if rate.get("effective_to") is None
            else _aware_utc(rate["effective_to"], "effective_to")
        )
        if start < effective_from < end:
            boundaries.add(effective_from)
        if effective_to is not None and start < effective_to < end:
            boundaries.add(effective_to)
    return boundaries


def _provisional_segments(
    interval: Mapping[str, Any],
    dimensions: Mapping[str, tuple[UsageDimensions, CapacityDimension]],
    rate_rows: Sequence[Mapping[str, Any]],
    start: datetime,
    end: datetime,
) -> tuple[tuple[datetime, datetime], ...]:
    boundaries = {start, end}
    cursor = _midnight(start.date() + timedelta(days=1))
    while cursor < end:
        boundaries.add(cursor)
        cursor += timedelta(days=1)
    boundaries.update(
        _matching_rate_boundaries(interval, dimensions, rate_rows, start, end)
    )
    ordered = sorted(boundaries)
    return tuple(zip(ordered, ordered[1:]))


def _clip_range(
    start: datetime,
    end: datetime | None,
    window_start: datetime,
    window_end: datetime,
) -> tuple[datetime, datetime] | None:
    clipped_start = max(start, window_start)
    clipped_end = window_end if end is None else min(end, window_end)
    return None if clipped_end <= clipped_start else (clipped_start, clipped_end)


def _merge_ranges(
    ranges: Sequence[tuple[datetime, datetime | None]],
    *,
    window_end: datetime,
) -> tuple[tuple[datetime, datetime | None], ...]:
    finite = sorted(
        (start, window_end if end is None else end) for start, end in ranges
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in finite:
        if end <= start:
            continue
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


class SourceAwareUsageReadModel:
    """Exact rolled-day + interval-tail reader, intentionally not runtime wired."""

    def __init__(
        self,
        audit_pool: asyncpg.Pool,
        app_pool: asyncpg.Pool,
        *,
        enabled_resources: Sequence[str] = _DEFAULT_ENABLED_RESOURCES,
        ide_workspace_pod_enabled: bool = False,
        storage_publication_policy: StoragePublicationPolicy | None = None,
    ) -> None:
        resources = tuple(dict.fromkeys(str(item) for item in enabled_resources))
        if not resources or any(not item for item in resources):
            raise ValueError("enabled_resources must contain non-empty names")
        unsupported = sorted(
            resource
            for resource in set(resources)
            if _resource_api_resources(resource) is None
        )
        if unsupported:
            raise ValueError(
                "usage read model has no inventory-scope mapping for: "
                + ", ".join(unsupported)
            )
        self._audit = audit_pool
        self._app = app_pool
        self._enabled_resources = resources
        self._ide_workspace_pod_enabled = bool(ide_workspace_pod_enabled)
        if storage_publication_policy is None:
            storage_publication_policy = StoragePublicationPolicy()
        if not isinstance(storage_publication_policy, StoragePublicationPolicy):
            raise ValueError(
                "storage_publication_policy must be a StoragePublicationPolicy"
            )
        self._storage_publication_policy = storage_publication_policy
        self._storage_publication_authorities = frozenset(
            storage_publication_policy.authorities
        )
        self._enabled_api_resources = frozenset().union(
            *(
                _resource_api_resources(resource) or frozenset()
                for resource in resources
            )
        )
        self._include_storage_asset_gaps = (
            "core/v1/persistentvolumes" in self._enabled_api_resources
        )

    def _ide_interval_filter(self) -> str:
        if self._ide_workspace_pod_enabled:
            return ""
        return (
            "AND NOT (interval.resource = 'workspace_pod' "
            "AND COALESCE(interval.details->>'product_class', '') = "
            "'ide-session')"
        )

    def _enabled_compute_activation_keys(self) -> tuple[str, ...]:
        keys: list[str] = []
        if "agent_pod" in self._enabled_resources:
            keys.append("agent_pod")
        if (
            "workspace_pod" in self._enabled_resources
            and self._ide_workspace_pod_enabled
        ):
            keys.append("ide_workspace_pod")
        if "workspace_vm" in self._enabled_resources:
            keys.append("workspace_vm")
        return tuple(keys)

    def _storage_tail_row_is_authorized(
        self,
        row: Mapping[str, Any],
        *,
        basis_field: str,
    ) -> bool:
        basis = str(row.get(basis_field) or "")
        if basis not in _STORAGE_BASIS_BY_API_RESOURCE.values():
            return True
        authority = _storage_authority_from_scope(row, basis_field=basis_field)
        if authority is None:
            return False
        interval_source_cluster = row.get("interval_source_cluster")
        if (
            interval_source_cluster is not None
            and interval_source_cluster != authority.source_cluster
        ):
            return False
        return authority in self._storage_publication_authorities

    async def read_app_snapshot(
        self,
        *,
        from_ts: datetime,
        to_ts: datetime,
        visibility: _Visibility,
        ref_id: str | None = None,
    ) -> AppUsageReadSnapshot:
        from_ts = _aware_utc(from_ts, "from_ts")
        to_ts = _aware_utc(to_ts, "to_ts")
        if to_ts <= from_ts:
            raise ValueError("usage window end must be after its start")

        async with self._app.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                control = await conn.fetchrow(_CONTROL_SQL)
                if control is None:
                    raise UsageReadContractError("metering control row is missing")
                if (
                    control["cutover_state"] != "active"
                    or control["cutover_at"] is None
                ):
                    raise UsageReadCutoverInactive(
                        "source-aware usage reads require active cutover"
                    )
                cutover_at = _aware_utc(control["cutover_at"], "cutover_at")
                watermark = control["last_closed_day"]
                if watermark is not None and not isinstance(watermark, date):
                    raise UsageReadContractError("rollup watermark is not a date")
                rolled_days = _whole_rolled_days(
                    from_ts,
                    to_ts,
                    watermark,
                    ref_id=ref_id,
                )

                daily_rows: Sequence[Mapping[str, Any]] = ()
                if rolled_days is not None:
                    daily_params: list[Any] = [rolled_days[0], rolled_days[1]]
                    daily_clauses: list[str] = []
                    _append_visibility(
                        daily_clauses,
                        daily_params,
                        visibility,
                        alias="usage",
                        ref_id=None,
                        ref_column=None,
                    )
                    daily_rows = await conn.fetch(
                        _DAILY_SQL.format(
                            visibility=_visibility_fragment(daily_clauses)
                        ),
                        *daily_params,
                    )

                rolled_cutoff = (
                    None
                    if watermark is None
                    else _midnight(watermark + timedelta(days=1))
                )
                tail_start = max(
                    from_ts,
                    cutover_at,
                    rolled_cutoff or from_ts,
                )
                intervals: Sequence[Mapping[str, Any]] = ()
                plan_rows: Sequence[Mapping[str, Any]] = ()
                plan_event_rows: Sequence[Mapping[str, Any]] = ()
                rate_rows: Sequence[Mapping[str, Any]] = ()
                if tail_start < to_ts:
                    interval_params: list[Any] = [
                        tail_start,
                        to_ts,
                        list(self._enabled_resources),
                        *self._storage_publication_policy.sql_columns(),
                    ]
                    interval_clauses: list[str] = []
                    _append_visibility(
                        interval_clauses,
                        interval_params,
                        visibility,
                        alias="interval",
                        ref_id=ref_id,
                        ref_column="interval.owner_id",
                        text_ref=True,
                    )
                    interval_visibility = _visibility_fragment(interval_clauses)
                    fetched_intervals = await conn.fetch(
                        _INTERVALS_SQL.format(
                            visibility=interval_visibility,
                            product_class=self._ide_interval_filter(),
                        ),
                        *interval_params,
                    )
                    intervals = tuple(
                        row
                        for row in fetched_intervals
                        if self._storage_tail_row_is_authorized(
                            row,
                            basis_field="measurement_basis",
                        )
                    )

                    plan_params: list[Any] = [
                        tail_start,
                        to_ts,
                        list(self._enabled_resources),
                        *self._storage_publication_policy.sql_columns(),
                    ]
                    plan_clauses: list[str] = []
                    _append_visibility(
                        plan_clauses,
                        plan_params,
                        visibility,
                        alias="interval",
                        ref_id=ref_id,
                        ref_column="interval.owner_id",
                        text_ref=True,
                    )
                    plan_visibility = _visibility_fragment(plan_clauses)
                    plan_rows = await conn.fetch(
                        _PLAN_HEADERS_SQL.format(
                            visibility=plan_visibility,
                            product_class=self._ide_interval_filter(),
                        ),
                        *plan_params,
                    )

                    correction_params: list[Any] = [
                        tail_start,
                        to_ts,
                        list(self._enabled_resources),
                        *self._storage_publication_policy.sql_columns(),
                    ]
                    correction_clauses: list[str] = []
                    _append_payload_visibility(
                        correction_clauses,
                        correction_params,
                        visibility,
                        alias="visible_event",
                        ref_id=ref_id,
                    )
                    correction_rows = await conn.fetch(
                        _CORRECTION_PLAN_HEADERS_SQL.format(
                            visibility=_visibility_fragment(correction_clauses),
                            product_class=self._ide_interval_filter(),
                        ),
                        *correction_params,
                    )
                    plan_rows = tuple(
                        row
                        for row in (*plan_rows, *correction_rows)
                        if self._storage_tail_row_is_authorized(
                            row,
                            basis_field="source_measurement_basis",
                        )
                    )
                    plan_ids = [
                        _as_uuid(str(row["id"]), "plan_id") for row in plan_rows
                    ]
                    plan_event_rows = await conn.fetch(
                        _PLAN_EVENTS_SQL,
                        plan_ids,
                    )
                    rate_rows = await conn.fetch(_RATES_SQL, tail_start, to_ts)

                # Rolled days carry their immutable coverage proof in
                # usage_rollup_day_state.  Mutable epoch health must only
                # govern the interval tail after the app rollup watermark;
                # otherwise a current outage would retroactively degrade a
                # day that was already sealed and rolled as complete.
                coverage_start = max(
                    from_ts,
                    cutover_at,
                    rolled_cutoff or from_ts,
                )
                epochs: Sequence[Mapping[str, Any]] = ()
                storage_requirements: Sequence[Mapping[str, Any]] = ()
                compute_requirements: Sequence[Mapping[str, Any]] = ()
                gaps: Sequence[Mapping[str, Any]] = ()
                if coverage_start < to_ts:
                    epochs = await conn.fetch(_EPOCHS_SQL, coverage_start, to_ts)
                    compute_keys = self._enabled_compute_activation_keys()
                    if compute_keys:
                        compute_requirements = await conn.fetch(
                            _COMPUTE_REQUIREMENTS_SQL,
                            list(compute_keys),
                            to_ts,
                        )
                    if self._storage_publication_policy.authorities:
                        storage_requirements = await conn.fetch(
                            _STORAGE_REQUIREMENTS_SQL,
                            *self._storage_publication_policy.sql_columns(),
                        )
                    epoch_ids = list(
                        dict.fromkeys(
                            [row["id"] for row in epochs]
                            + [
                                row["inventory_scope_epoch_id"]
                                for row in compute_requirements
                                if row.get("inventory_scope_epoch_id") is not None
                            ]
                        )
                    )
                    if epoch_ids:
                        gaps = await conn.fetch(
                            (
                                _STORAGE_GAPS_SQL
                                if self._include_storage_asset_gaps
                                else _GAPS_SQL
                            ),
                            epoch_ids,
                            coverage_start,
                            to_ts,
                        )

                day_states: Sequence[Mapping[str, Any]] = ()
                if watermark is not None:
                    low_day = from_ts.date()
                    high_day = min(
                        watermark,
                        (to_ts - timedelta(microseconds=1)).date(),
                    )
                    if low_day <= high_day:
                        day_states = await conn.fetch(
                            _DAY_STATES_SQL,
                            low_day,
                            high_day,
                        )

        events_by_plan: dict[uuid.UUID, list[Mapping[str, Any]]] = defaultdict(list)
        for event in plan_event_rows:
            events_by_plan[_as_uuid(str(event["plan_id"]), "plan_id")].append(event)
        plans: list[FrozenPublicationPlan] = []
        for plan_row in plan_rows:
            plan_id = _as_uuid(str(plan_row["id"]), "plan_id")
            try:
                plans.append(
                    FrozenPublicationPlan.from_records(
                        plan_row,
                        events_by_plan.get(plan_id, ()),
                    )
                )
            except PublicationContractError as exc:
                raise UsageReadContractError(
                    "frozen publication plan failed read verification"
                ) from exc

        return AppUsageReadSnapshot(
            cutover_at=cutover_at,
            watermark=watermark,
            rolled_days=rolled_days,
            daily_rows=tuple(dict(row) for row in daily_rows),
            intervals=tuple(dict(row) for row in intervals),
            plans=tuple(plans),
            rate_rows=tuple(dict(row) for row in rate_rows),
            epochs=tuple(dict(row) for row in epochs),
            storage_requirements=tuple(dict(row) for row in storage_requirements),
            gaps=tuple(dict(row) for row in gaps),
            day_states=tuple(dict(row) for row in day_states),
            compute_requirements=tuple(dict(row) for row in compute_requirements),
        )

    async def summary(
        self,
        *,
        from_ts: datetime,
        to_ts: datetime,
        visibility: _Visibility,
        ref_id: str | None = None,
        as_of: datetime | None = None,
    ) -> UsageSummaryV2:
        from_ts = _aware_utc(from_ts, "from_ts")
        to_ts = _aware_utc(to_ts, "to_ts")
        if to_ts <= from_ts:
            raise ValueError("usage window end must be after its start")
        observed_at = _aware_utc(as_of or datetime.now(_UTC), "as_of")
        snapshot = await self.read_app_snapshot(
            from_ts=from_ts,
            to_ts=to_ts,
            visibility=visibility,
            ref_id=ref_id,
        )

        totals: dict[UsageDimensions, _UsageTotals] = {}
        self._add_daily(totals, snapshot.daily_rows)
        audit_rows = await self._read_audit_tail(
            from_ts=from_ts,
            to_ts=to_ts,
            visibility=visibility,
            ref_id=ref_id,
            snapshot=snapshot,
        )
        self._add_finalized_rows(totals, audit_rows)
        conflict_ranges = self._add_interval_tail(
            totals,
            snapshot,
            from_ts=from_ts,
            to_ts=to_ts,
            visibility=visibility,
            ref_id=ref_id,
        )

        rows = self._render_rows(totals)
        coverage = self._coverage(
            snapshot,
            from_ts=from_ts,
            to_ts=to_ts,
            conflict_ranges=conflict_ranges,
        )
        includes_provisional = any(
            Decimal(row.confirmed_provisional_quantity) != 0 for row in rows
        )
        return UsageSummaryV2(
            window=UsageWindowV2(
                start=from_ts,
                end=to_ts,
                as_of=observed_at,
                data_through=coverage.data_through,
            ),
            rows=rows,
            coverage=UsageCoverageV2(
                status=coverage.status,
                includes_provisional=includes_provisional,
                required_sources_ok=coverage.required_sources_ok,
                required_sources_total=coverage.required_sources_total,
                unknown_ranges=[
                    {"start": start, "end": end}
                    for start, end in coverage.unknown_ranges
                ],
                excluded_domains=list(_BASE_EXCLUDED_DOMAINS),
            ),
        )

    async def _read_audit_tail(
        self,
        *,
        from_ts: datetime,
        to_ts: datetime,
        visibility: _Visibility,
        ref_id: str | None,
        snapshot: AppUsageReadSnapshot,
    ) -> tuple[Mapping[str, Any], ...]:
        full_start = full_end = None
        if snapshot.rolled_days is not None:
            full_start = _midnight(snapshot.rolled_days[0])
            full_end = _midnight(snapshot.rolled_days[1] + timedelta(days=1))
        params: list[Any] = [
            from_ts,
            to_ts,
            snapshot.rolled_cutoff,
            full_start,
            full_end,
            visibility.include_non_customer,
            list(_INFRA_SOURCES),
            list(self._enabled_resources),
        ]
        clauses: list[str] = []
        _append_visibility(
            clauses,
            params,
            visibility,
            alias="event",
            ref_id=ref_id,
            ref_column="event.ref_id",
            apply_attribution_filter=False,
        )
        rows = await self._audit.fetch(
            _AUDIT_SQL.format(visibility=_visibility_fragment(clauses)),
            *params,
        )
        return tuple(dict(row) for row in rows)

    @staticmethod
    def _totals_for(
        totals: dict[UsageDimensions, _UsageTotals],
        dimensions: UsageDimensions,
    ) -> _UsageTotals:
        return totals.setdefault(dimensions, _UsageTotals())

    def _add_daily(
        self,
        totals: dict[UsageDimensions, _UsageTotals],
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        self._add_finalized_rows(totals, rows)

    def _add_finalized_rows(
        self,
        totals: dict[UsageDimensions, _UsageTotals],
        rows: Sequence[Mapping[str, Any]],
    ) -> None:
        for row in rows:
            dimensions = UsageDimensions.from_mapping(row)
            target = self._totals_for(totals, dimensions)
            priced_quantity = _decimal(
                row.get("priced_quantity") or 0,
                "priced_quantity",
            )
            unpriced_quantity = _decimal(
                row.get("unpriced_quantity") or 0,
                "unpriced_quantity",
            )
            priced_events = int(row.get("priced_events") or 0)
            unpriced_events = int(row.get("unpriced_events") or 0)
            amount = row.get("cost_usd")
            target.add_finalized(
                priced_quantity,
                cost_usd=(
                    Decimal(0)
                    if amount is None and priced_events > 0 and priced_quantity == 0
                    else None
                    if amount is None
                    else _decimal(amount, "cost_usd")
                ),
                events=priced_events,
            )
            target.add_finalized(
                unpriced_quantity,
                cost_usd=None,
                events=unpriced_events,
            )

    def _add_interval_tail(
        self,
        totals: dict[UsageDimensions, _UsageTotals],
        snapshot: AppUsageReadSnapshot,
        *,
        from_ts: datetime,
        to_ts: datetime,
        visibility: _Visibility,
        ref_id: str | None,
    ) -> tuple[tuple[datetime, datetime | None], ...]:
        tail_start = max(
            from_ts,
            snapshot.cutover_at,
            snapshot.rolled_cutoff or from_ts,
        )
        if tail_start >= to_ts:
            return ()

        interval_by_id = {
            _as_uuid(str(row["id"]), "interval_id"): row for row in snapshot.intervals
        }
        compute_authority_by_epoch: dict[
            tuple[str, uuid.UUID, uuid.UUID], Mapping[str, Any]
        ] = {}
        for requirement in snapshot.compute_requirements:
            raw_scope_id = requirement.get("inventory_scope_id")
            raw_epoch_id = requirement.get("inventory_scope_epoch_id")
            if raw_scope_id is None or raw_epoch_id is None:
                continue
            identity = (
                str(requirement.get("activation_key") or ""),
                _as_uuid(str(raw_scope_id), "compute inventory_scope_id"),
                _as_uuid(str(raw_epoch_id), "compute inventory_scope_epoch_id"),
            )
            if identity in compute_authority_by_epoch:
                raise UsageReadContractError("compute interval authority is duplicated")
            compute_authority_by_epoch[identity] = requirement
        compute_authority_end_by_interval: dict[uuid.UUID, datetime | None] = {}
        for interval_id, interval in interval_by_id.items():
            activation_key = _compute_activation_key_from_interval(interval)
            if activation_key is None:
                continue
            scope_id = _as_uuid(
                str(interval["inventory_scope_id"]),
                "interval inventory_scope_id",
            )
            raw_epoch_id = interval.get("compute_scope_epoch_id")
            if raw_epoch_id is None:
                raise UsageReadContractError(
                    "Slice 3 interval lacks its immutable epoch binding"
                )
            epoch_id = _as_uuid(
                str(raw_epoch_id),
                "interval compute_scope_epoch_id",
            )
            requirement = compute_authority_by_epoch.get(
                (activation_key, scope_id, epoch_id)
            )
            if requirement is None:
                raise UsageReadContractError(
                    "Slice 3 interval lacks exact class epoch authority"
                )
            effective_boundary = max(
                _aware_utc(requirement["activated_at"], "activated_at"),
                _aware_utc(
                    requirement["authority_effective_from"],
                    "authority_effective_from",
                ),
            )
            if _aware_utc(interval["started_at"], "started_at") < effective_boundary:
                raise UsageReadContractError(
                    "Slice 3 interval predates exact class epoch authority"
                )
            compute_authority_end_by_interval[interval_id] = (
                None
                if requirement.get("retired_at") is None
                else _aware_utc(requirement["retired_at"], "retired_at")
            )
        dimensions_by_interval = {
            interval_id: _interval_dimensions(row)
            for interval_id, row in interval_by_id.items()
        }
        published_ranges: dict[uuid.UUID, list[tuple[datetime, datetime]]] = (
            defaultdict(list)
        )
        conflict_ranges: list[tuple[datetime, datetime | None]] = []

        for plan in snapshot.plans:
            authority_end = compute_authority_end_by_interval.get(
                plan.source_interval_id
            )
            if authority_end is not None and plan.period_end > authority_end:
                raise UsageReadContractError(
                    "Slice 3 publication plan exceeds exact epoch authority"
                )
            overlap = _clip_range(
                plan.period_start,
                plan.period_end,
                tail_start,
                to_ts,
            )
            if overlap is None:
                continue
            if plan.state == "conflict":
                conflict_ranges.append(overlap)
                continue
            if plan.state != "published":
                # Planned includes the valid audit-committed/app-CAS crash
                # window.  The interval cursor has not advanced, so this time
                # remains provisional and exposes no canonical cost.
                continue

            if plan.plan_kind == "correction":
                # A correction never participates in cursor contiguity.  Its
                # negative old-attribution and positive replacement events are
                # append-only deltas over an already-finalized ordinary/late
                # segment.  The complete plan was hash-verified before this
                # per-event visibility filter was applied.
                for planned_event in plan.events:
                    payload = planned_event.event.payload
                    if not _payload_is_visible(payload, visibility, ref_id=ref_id):
                        continue
                    self._add_published_payload(
                        totals,
                        payload,
                        capacity=_payload_capacity(payload),
                        tail_start=tail_start,
                        to_ts=to_ts,
                    )
                continue

            if plan.plan_kind not in {"usage", "late-usage"}:
                raise UsageReadContractError("publication plan kind is unsupported")
            interval = interval_by_id.get(plan.source_interval_id)
            if interval is None:
                raise UsageReadContractError(
                    "cursor-advancing plan has no visible source interval"
                )
            materialized_through = _aware_utc(
                interval["materialized_through"],
                "materialized_through",
            )
            if plan.period_end > materialized_through:
                raise UsageReadContractError(
                    "published plan extends beyond the interval cursor"
                )
            published_ranges[plan.source_interval_id].append(
                (plan.period_start, plan.period_end)
            )
            interval_dimensions = dimensions_by_interval[plan.source_interval_id]
            seen_units: set[str] = set()
            for planned_event in plan.events:
                payload = planned_event.event.payload
                _validate_payload_attribution(payload, interval)
                dimensions = UsageDimensions.from_mapping(payload)
                pair = interval_dimensions.get(dimensions.unit)
                if pair is None or pair[0] != dimensions:
                    raise UsageReadContractError(
                        "publication payload dimensions differ from its interval"
                    )
                if dimensions.unit in seen_units:
                    raise UsageReadContractError(
                        "publication plan repeats an interval dimension"
                    )
                seen_units.add(dimensions.unit)
                capacity = pair[1]
                if (
                    str(payload["source_capacity_value"]) != str(capacity.capacity)
                    or payload["source_capacity_unit"] != capacity.capacity_unit
                ):
                    raise UsageReadContractError(
                        "publication payload capacity differs from its interval"
                    )
                self._add_published_payload(
                    totals,
                    payload,
                    capacity=capacity,
                    tail_start=tail_start,
                    to_ts=to_ts,
                )
            if seen_units != set(interval_dimensions):
                raise UsageReadContractError(
                    "publication plan does not cover every interval dimension"
                )

        for interval_id, interval in interval_by_id.items():
            started_at = _aware_utc(interval["started_at"], "started_at")
            materialized_through = _aware_utc(
                interval["materialized_through"],
                "materialized_through",
            )
            authority_end = compute_authority_end_by_interval.get(interval_id)
            if authority_end is not None and materialized_through > authority_end:
                raise UsageReadContractError(
                    "Slice 3 interval cursor exceeds exact epoch authority"
                )
            finalized_start = max(started_at, tail_start)
            finalized_end = min(materialized_through, to_ts)
            if finalized_start < finalized_end:
                self._require_contiguous_publication(
                    interval_id,
                    finalized_start,
                    finalized_end,
                    published_ranges.get(interval_id, ()),
                )

            last_confirmed_at = _aware_utc(
                interval["last_confirmed_at"],
                "last_confirmed_at",
            )
            ended_at = (
                None
                if interval.get("ended_at") is None
                else _aware_utc(interval["ended_at"], "ended_at")
            )
            provisional_start = max(materialized_through, started_at, tail_start)
            provisional_end = min(
                to_ts,
                last_confirmed_at,
                ended_at or last_confirmed_at,
                compute_authority_end_by_interval.get(interval_id) or to_ts,
            )
            if provisional_start >= provisional_end:
                continue
            interval_dimensions = dimensions_by_interval[interval_id]
            for segment_start, segment_end in _provisional_segments(
                interval,
                interval_dimensions,
                snapshot.rate_rows,
                provisional_start,
                provisional_end,
            ):
                for dimensions, capacity in interval_dimensions.values():
                    try:
                        quantity = capacity_quantity(
                            capacity,
                            segment_start,
                            segment_end,
                        )
                    except PublicationContractError as exc:
                        raise UsageReadContractError(
                            "provisional overlap quantity is invalid"
                        ) from exc
                    self._totals_for(totals, dimensions).add_provisional(quantity)

        return tuple(conflict_ranges)

    def _add_published_payload(
        self,
        totals: dict[UsageDimensions, _UsageTotals],
        payload: Mapping[str, Any],
        *,
        capacity: CapacityDimension,
        tail_start: datetime,
        to_ts: datetime,
    ) -> None:
        dimensions = UsageDimensions.from_mapping(payload)
        if dimensions.unit != capacity.unit:
            raise UsageReadContractError(
                "publication payload unit differs from its capacity dimension"
            )
        event_start = _payload_timestamp(payload["period_start"], "period_start")
        event_end = _payload_timestamp(payload["period_end"], "period_end")
        event_overlap = _clip_range(
            event_start,
            event_end,
            tail_start,
            to_ts,
        )
        if event_overlap is None:
            return

        stored_quantity = _decimal(payload["quantity"], "quantity")
        try:
            unsigned_expected = capacity_quantity(
                capacity,
                event_start,
                event_end,
            )
        except PublicationContractError as exc:
            raise UsageReadContractError(
                "publication event capacity quantity is invalid"
            ) from exc
        expected_quantity = (
            -unsigned_expected if stored_quantity < 0 else unsigned_expected
        )
        if stored_quantity != expected_quantity:
            raise UsageReadContractError(
                "publication event quantity differs from exact signed capacity"
            )

        raw_rate = payload.get("rate_usd")
        raw_cost = payload.get("cost_usd")
        rate = None if raw_rate is None else _decimal(raw_rate, "rate_usd")
        if rate is None:
            if raw_cost is not None:
                raise UsageReadContractError(
                    "unpriced publication event carries a frozen cost"
                )
        else:
            if raw_cost is None:
                raise UsageReadContractError(
                    "priced publication event is missing its frozen cost"
                )
            expected_cost = _multiply(stored_quantity, rate)
            if _decimal(raw_cost, "cost_usd") != expected_cost:
                raise UsageReadContractError(
                    "publication event cost differs from frozen rate"
                )

        if event_overlap == (event_start, event_end):
            quantity = stored_quantity
        else:
            try:
                quantity = capacity_quantity(
                    capacity,
                    event_overlap[0],
                    event_overlap[1],
                )
            except PublicationContractError as exc:
                raise UsageReadContractError(
                    "publication overlap quantity is invalid"
                ) from exc
            if stored_quantity < 0:
                quantity = -quantity

        if rate is None:
            cost = None
        elif event_overlap == (event_start, event_end):
            cost = _decimal(raw_cost, "cost_usd")
        else:
            cost = _multiply(quantity, rate)
        self._totals_for(totals, dimensions).add_finalized(
            quantity,
            cost_usd=cost,
            events=1,
        )

    @staticmethod
    def _require_contiguous_publication(
        interval_id: uuid.UUID,
        expected_start: datetime,
        expected_end: datetime,
        ranges: Sequence[tuple[datetime, datetime]],
    ) -> None:
        cursor = expected_start
        for range_start, range_end in sorted(ranges):
            clipped = _clip_range(
                range_start,
                range_end,
                expected_start,
                expected_end,
            )
            if clipped is None:
                continue
            if clipped[0] != cursor:
                problem = "gap" if clipped[0] > cursor else "overlap"
                raise UsageReadContractError(
                    f"published plans have a finalized {problem} for interval "
                    f"{interval_id}"
                )
            cursor = clipped[1]
        if cursor != expected_end:
            raise UsageReadContractError(
                f"published plans do not cover finalized interval {interval_id}"
            )

    @staticmethod
    def _render_rows(
        totals: Mapping[UsageDimensions, _UsageTotals],
    ) -> list[UsageRowV2]:
        rendered: list[UsageRowV2] = []
        for dimensions, values in sorted(
            totals.items(),
            key=lambda item: (
                item[0].category,
                item[0].measurement_basis,
                item[0].resource_class,
                item[0].resource,
                item[0].unit,
                item[0].attribution_scope,
                item[0].cost_domain,
                item[0].measurement_algorithm,
            ),
        ):
            priced = Decimal(decimal_text(values.finalized_priced))
            final_unpriced = Decimal(decimal_text(values.finalized_unpriced))
            provisional = Decimal(decimal_text(values.provisional))
            finalized = _add(priced, final_unpriced)
            unpriced = _add(final_unpriced, provisional)
            quantity = _add(priced, unpriced)
            if min(priced, final_unpriced, provisional, finalized, quantity) < 0:
                raise UsageReadContractError(
                    "net usage/finality/pricing buckets cannot be negative"
                )
            amount = None
            if priced > 0 or (priced == 0 and unpriced == 0 and values.priced_events):
                amount = decimal_text(values.cost_usd)
            rendered.append(
                UsageRowV2(
                    category=dimensions.category,
                    measurement_basis=dimensions.measurement_basis,
                    cost_domain=dimensions.cost_domain,
                    resource_class=dimensions.resource_class,
                    measurement_algorithm=dimensions.measurement_algorithm,
                    resource=dimensions.resource,
                    unit=dimensions.unit,
                    attribution_scope=dimensions.attribution_scope,
                    quantity=decimal_text(quantity),
                    finalized_quantity=decimal_text(finalized),
                    confirmed_provisional_quantity=decimal_text(provisional),
                    ledger_cost=ledger_cost(
                        amount=amount,
                        priced_quantity=priced,
                        unpriced_quantity=unpriced,
                        priced_events=values.priced_events,
                        unpriced_events=values.unpriced_events,
                    ),
                    events=values.events,
                )
            )
        return rendered

    def _coverage(
        self,
        snapshot: AppUsageReadSnapshot,
        *,
        from_ts: datetime,
        to_ts: datetime,
        conflict_ranges: Sequence[tuple[datetime, datetime | None]],
    ) -> _CoverageResult:
        coverage_start = max(from_ts, snapshot.cutover_at)
        unknown: list[tuple[datetime, datetime | None]] = list(conflict_ranges)
        rolled_boundary = snapshot.rolled_cutoff
        rolled_end = (
            coverage_start
            if rolled_boundary is None
            else min(to_ts, max(coverage_start, rolled_boundary))
        )
        rolled_complete = True
        day_state_by_day: dict[date, Mapping[str, Any]] = {}
        for state in snapshot.day_states:
            state_day = state.get("day")
            if not isinstance(state_day, date):
                raise UsageReadContractError("rolled day state has invalid day")
            if state_day in day_state_by_day:
                raise UsageReadContractError("rolled day state is duplicated")
            day_state_by_day[state_day] = state

        if rolled_end > coverage_start:
            first_required_day = coverage_start.date()
            last_required_day = (rolled_end - timedelta(microseconds=1)).date()
            current_day = first_required_day
            while current_day <= last_required_day:
                day_start = max(_midnight(current_day), coverage_start)
                day_end = min(
                    _midnight(current_day + timedelta(days=1)),
                    rolled_end,
                )
                state = day_state_by_day.get(current_day)
                if state is None:
                    rolled_complete = False
                    unknown.append((day_start, day_end))
                    current_day += timedelta(days=1)
                    continue

                rollup_revision = state.get("infra_coverage_revision")
                current_revision = state.get("current_infra_coverage_revision")
                if (
                    state.get("infra_state") != "sealed"
                    or not isinstance(rollup_revision, str)
                    or not rollup_revision
                    or not isinstance(current_revision, str)
                    or not current_revision
                    or rollup_revision != current_revision
                ):
                    # A sealed proof may acquire late fail-closed evidence after
                    # this daily row was built.  Until the typed rollup consumes
                    # that exact revision, none of the rolled intersection is a
                    # trustworthy completeness proof.
                    rolled_complete = False
                    unknown.append((day_start, day_end))
                    current_day += timedelta(days=1)
                    continue

                status = str(state.get("coverage_status"))
                if status not in {"complete", "partial"}:
                    raise UsageReadContractError(
                        "rolled day coverage status is invalid"
                    )
                raw_ranges = state.get("unknown_ranges")
                if isinstance(raw_ranges, str):
                    try:
                        raw_ranges = json.loads(raw_ranges)
                    except json.JSONDecodeError as exc:
                        raise UsageReadContractError(
                            "rolled day unknown_ranges is invalid JSON"
                        ) from exc
                if not isinstance(raw_ranges, list):
                    raise UsageReadContractError(
                        "rolled day unknown_ranges must be an array"
                    )
                durable_day_unknown = False
                query_unknown = False
                full_day_start = _midnight(current_day)
                full_day_end = _midnight(current_day + timedelta(days=1))
                for item in raw_ranges:
                    if not isinstance(item, Mapping) or "start" not in item:
                        raise UsageReadContractError(
                            "rolled day unknown range has invalid shape"
                        )
                    start = _payload_timestamp(item["start"], "unknown_range.start")
                    raw_end = item.get("end")
                    end = (
                        None
                        if raw_end is None
                        else _payload_timestamp(raw_end, "unknown_range.end")
                    )
                    durable_range = _clip_range(
                        start,
                        end,
                        full_day_start,
                        full_day_end,
                    )
                    if durable_range is None:
                        continue
                    durable_day_unknown = True
                    clipped = _clip_range(
                        durable_range[0],
                        durable_range[1],
                        day_start,
                        day_end,
                    )
                    if clipped is not None:
                        unknown.append(clipped)
                        query_unknown = True
                if query_unknown:
                    rolled_complete = False
                if status == "partial" and not durable_day_unknown:
                    # A partial durable decision without a usable range cannot
                    # prove any part of the day.  Degrade the whole intersection
                    # instead of letting data_through cross an unspecified gap.
                    unknown.append((day_start, day_end))
                    rolled_complete = False
                current_day += timedelta(days=1)

        if coverage_start >= to_ts:
            return _CoverageResult(
                status="partial",
                data_through=None,
                required_sources_ok=0,
                required_sources_total=0,
                unknown_ranges=_merge_ranges(unknown, window_end=to_ts),
            )

        live_start = max(
            coverage_start,
            rolled_boundary or coverage_start,
        )
        epochs_by_scope: dict[uuid.UUID, list[Mapping[str, Any]]] = defaultdict(list)
        for epoch in snapshot.epochs:
            epochs_by_scope[_as_uuid(str(epoch["scope_id"]), "scope_id")].append(epoch)

        expected_storage_authorities = set(self._storage_publication_policy.authorities)
        storage_requirements: set[StorageCoverageRequirement] = set()
        try:
            for row in snapshot.storage_requirements:
                requirement = StorageCoverageRequirement.from_mapping(row)
                if requirement in storage_requirements:
                    raise UsageReadContractError(
                        "storage coverage requirement is duplicated"
                    )
                if requirement.authority not in expected_storage_authorities:
                    raise UsageReadContractError(
                        "storage coverage requirement is outside process policy"
                    )
                storage_requirements.add(requirement)
        except PublicationContractError as exc:
            raise UsageReadContractError(
                "storage coverage requirement is invalid"
            ) from exc

        requirements_by_authority: dict[
            StoragePublicationAuthority, list[StorageCoverageRequirement]
        ] = defaultdict(list)
        for requirement in storage_requirements:
            requirements_by_authority[requirement.authority].append(requirement)

        missing_storage_authorities: set[StoragePublicationAuthority] = set()
        for authority in expected_storage_authorities:
            authority_requirements = requirements_by_authority.get(authority, ())
            if not authority_requirements:
                missing_storage_authorities.add(authority)
                continue
            roles = {
                requirement.requirement_role for requirement in authority_requirements
            }
            expected_roles = (
                {"quantity"}
                if authority.measurement_basis == "claim-requested"
                else {"quantity", "attribution"}
            )
            if roles != expected_roles:
                raise UsageReadContractError(
                    "storage authority has an incomplete coverage requirement set"
                )

        active_storage_requirements = tuple(
            requirement
            for requirement in storage_requirements
            if requirement.effective_from < to_ts
        )
        requirements_by_scope: dict[uuid.UUID, list[StorageCoverageRequirement]] = (
            defaultdict(list)
        )
        for requirement in active_storage_requirements:
            requirements_by_scope[requirement.inventory_scope_id].append(requirement)

        epoch_by_id = {
            _as_uuid(str(epoch["id"]), "scope_epoch_id"): epoch
            for epoch in snapshot.epochs
        }
        gaps_by_epoch: dict[uuid.UUID, list[tuple[datetime, datetime | None]]] = (
            defaultdict(list)
        )
        compute_gaps_by_authority: dict[
            tuple[str, uuid.UUID], list[tuple[datetime, datetime | None]]
        ] = defaultdict(list)
        enabled_compute_keys = frozenset(self._enabled_compute_activation_keys())
        for gap in snapshot.gaps:
            start = _aware_utc(gap["gap_start"], "gap_start")
            end = (
                None
                if gap.get("gap_end") is None
                else _aware_utc(gap["gap_end"], "gap_end")
            )
            epoch_id = _as_uuid(str(gap["scope_epoch_id"]), "scope_epoch_id")
            reason = str(gap.get("reason") or "")
            compute_key: str | None = None
            if reason.startswith(_COMPUTE_AUTHORITY_GAP_PREFIX):
                compute_key = reason.removeprefix(_COMPUTE_AUTHORITY_GAP_PREFIX)
                if compute_key not in enabled_compute_keys:
                    continue
            gap_floor = live_start
            epoch = epoch_by_id.get(epoch_id)
            if compute_key is None and epoch is not None:
                scope_id = _as_uuid(str(epoch["scope_id"]), "scope_id")
                scope_requirements = requirements_by_scope.get(scope_id, ())
                if scope_requirements:
                    gap_floor = max(
                        gap_floor,
                        min(
                            requirement.effective_from
                            for requirement in scope_requirements
                        ),
                    )
            clipped = _clip_range(start, end, gap_floor, to_ts)
            if clipped is None:
                continue
            if compute_key is None:
                gaps_by_epoch[epoch_id].append(clipped)
            else:
                compute_gaps_by_authority[(compute_key, epoch_id)].append(clipped)
            unknown.append(clipped)

        scope_through: list[datetime] = []
        ok = 0
        total = 0

        # Stable inventory scope IDs are shared by agent/IDE Pods. Each class
        # owns an append-only sequence of exact epoch authorities; coverage may
        # resume after explicit recovery promotion, but the interval between a
        # predecessor retirement and successor effective time stays unknown.
        compute_groups: dict[tuple[str, uuid.UUID], list[Mapping[str, Any]]] = (
            defaultdict(list)
        )
        for requirement in snapshot.compute_requirements:
            activation_key = str(requirement.get("activation_key") or "")
            if activation_key not in _COMPUTE_API_RESOURCE:
                raise UsageReadContractError(
                    "compute coverage requirement has an invalid activation key"
                )
            raw_scope_id = requirement.get("inventory_scope_id")
            activation_boundary = _aware_utc(
                requirement["activated_at"],
                f"{activation_key} activated_at",
            )
            if raw_scope_id is None:
                fallback_start = max(live_start, activation_boundary)
                total += 1
                scope_through.append(fallback_start)
                unknown.append((fallback_start, to_ts))
                continue
            scope_id = _as_uuid(str(raw_scope_id), "compute inventory_scope_id")
            compute_groups[(activation_key, scope_id)].append(requirement)

        for (activation_key, scope_id), authorities in sorted(
            compute_groups.items(), key=lambda item: (item[0][0], str(item[0][1]))
        ):
            total += 1
            expected_api_resource = _COMPUTE_API_RESOURCE[activation_key]
            activation_boundary = _aware_utc(
                authorities[0]["activated_at"],
                f"{activation_key} activated_at",
            )
            fallback_start = max(live_start, activation_boundary)
            scope_ok = True
            scope_watermark = to_ts
            coverage_cursor = fallback_start
            seen_epochs: set[uuid.UUID] = set()
            ordered = sorted(
                authorities,
                key=lambda row: int(row.get("authority_sequence") or 0),
            )
            for authority in ordered:
                if (
                    _aware_utc(
                        authority["activated_at"],
                        f"{activation_key} activated_at",
                    )
                    != activation_boundary
                ):
                    raise UsageReadContractError(
                        "compute authority sequence changed activation boundary"
                    )
                raw_epoch_id = authority.get("inventory_scope_epoch_id")
                raw_effective_from = authority.get("authority_effective_from")
                if raw_epoch_id is None or raw_effective_from is None:
                    continue
                epoch_id = _as_uuid(str(raw_epoch_id), "compute scope_epoch_id")
                if epoch_id in seen_epochs:
                    raise UsageReadContractError(
                        "compute epoch authority is duplicated"
                    )
                seen_epochs.add(epoch_id)
                if str(authority.get("api_resource") or "") != expected_api_resource:
                    raise UsageReadContractError(
                        "compute epoch authority resource is inconsistent"
                    )
                authority_start = max(
                    fallback_start,
                    _aware_utc(
                        raw_effective_from,
                        f"{activation_key} authority_effective_from",
                    ),
                )
                retired_at = (
                    None
                    if authority.get("retired_at") is None
                    else _aware_utc(
                        authority["retired_at"],
                        f"{activation_key} retired_at",
                    )
                )
                authority_end = min(to_ts, retired_at or to_ts)
                if authority_end <= fallback_start or authority_start >= to_ts:
                    continue
                if authority_start > coverage_cursor:
                    unknown.append((coverage_cursor, authority_start))
                    scope_ok = False
                    scope_watermark = min(scope_watermark, coverage_cursor)
                epoch_gaps = tuple(gaps_by_epoch.get(epoch_id, ())) + tuple(
                    compute_gaps_by_authority.get((activation_key, epoch_id), ())
                )
                if str(authority.get("item_health")) != "healthy" or (
                    str(authority.get("continuity_health")) != "healthy"
                    and not epoch_gaps
                ):
                    unknown.append((authority_start, authority_end))
                    scope_ok = False
                    scope_watermark = min(scope_watermark, authority_start)
                complete_through = authority.get("complete_through")
                complete_end = authority_start
                if complete_through is not None:
                    complete_end = max(
                        authority_start,
                        min(
                            authority_end,
                            _aware_utc(complete_through, "complete_through"),
                        ),
                    )
                if complete_end < authority_end:
                    unknown.append((complete_end, authority_end))
                    scope_ok = False
                    scope_watermark = min(scope_watermark, complete_end)
                for gap_start, _gap_end in epoch_gaps:
                    if gap_start < authority_end:
                        scope_ok = False
                        scope_watermark = min(
                            scope_watermark,
                            max(authority_start, gap_start),
                        )
                coverage_cursor = max(coverage_cursor, authority_end)
            if coverage_cursor < to_ts:
                unknown.append((coverage_cursor, to_ts))
                scope_ok = False
                scope_watermark = min(scope_watermark, coverage_cursor)
            scope_through.append(scope_watermark)
            if scope_ok:
                ok += 1

        seen_storage_scopes: set[uuid.UUID] = set()
        for scope_id, epochs in epochs_by_scope.items():
            scope_ok = True
            scope_watermark = to_ts
            saw_effective_epoch = False
            scope_requirements = requirements_by_scope.get(scope_id, ())
            api_resources = {str(epoch.get("api_resource") or "") for epoch in epochs}
            if len(api_resources) != 1:
                raise UsageReadContractError(
                    "inventory scope epochs disagree on api_resource"
                )
            api_resource = next(iter(api_resources))
            is_storage_scope = api_resource in _STORAGE_BASIS_BY_API_RESOURCE
            storage_requirement_start: datetime | None = None
            storage_scope_enabled = True
            if is_storage_scope:
                storage_scope_enabled = bool(scope_requirements)
                if scope_requirements:
                    storage_requirement_start = max(
                        live_start,
                        min(
                            requirement.effective_from
                            for requirement in scope_requirements
                        ),
                    )
                    for requirement in scope_requirements:
                        if requirement.expected_api_resource != api_resource:
                            raise UsageReadContractError(
                                "storage coverage requirement resource mismatch"
                            )
                        quantity_resource = (
                            requirement.expected_api_resource
                            if requirement.requirement_role == "quantity"
                            else "core/v1/persistentvolumes"
                        )
                        if quantity_resource not in self._enabled_api_resources:
                            storage_scope_enabled = False
                else:
                    storage_requirement_start = live_start

            coverage_cursor = storage_requirement_start
            for epoch in sorted(epochs, key=lambda row: row["required_from"]):
                epoch_id = _as_uuid(str(epoch["id"]), "scope_epoch_id")
                effective_start = max(
                    storage_requirement_start or live_start,
                    _aware_utc(epoch["required_from"], "required_from"),
                )
                effective_end = (
                    to_ts
                    if epoch.get("retired_at") is None
                    else min(
                        to_ts,
                        _aware_utc(epoch["retired_at"], "retired_at"),
                    )
                )
                if effective_end <= effective_start:
                    continue
                if coverage_cursor is not None and effective_start > coverage_cursor:
                    unknown.append((coverage_cursor, effective_start))
                    scope_ok = False
                    scope_watermark = min(scope_watermark, coverage_cursor)
                saw_effective_epoch = True
                source_enabled = (
                    storage_scope_enabled
                    if is_storage_scope
                    else api_resource in self._enabled_api_resources
                )
                item_healthy = str(epoch.get("item_health")) == "healthy"
                continuity_healthy = str(epoch.get("continuity_health")) == "healthy"
                epoch_gaps = gaps_by_epoch.get(epoch_id, ())
                if (
                    not source_enabled
                    or not item_healthy
                    or (not continuity_healthy and not epoch_gaps)
                ):
                    # Persisted gap rows are the interval-specific continuity
                    # evidence.  Do not let the epoch's mutable aggregate
                    # ``gap`` health retroactively invalidate otherwise known
                    # time; if that evidence is absent, fail closed instead.
                    # Snapshot/backend health describe present freshness, so
                    # their historical boundary is ``complete_through`` below.
                    # Item health has no equivalent item-specific read proof
                    # yet and therefore remains a whole-epoch blocker.
                    scope_ok = False
                    unknown.append((effective_start, effective_end))
                    scope_watermark = min(scope_watermark, effective_start)
                complete_through = epoch.get("complete_through")
                complete_end = effective_start
                if complete_through is not None:
                    complete_end = max(
                        effective_start,
                        min(
                            effective_end,
                            _aware_utc(complete_through, "complete_through"),
                        ),
                    )
                if complete_end < effective_end:
                    unknown.append((complete_end, effective_end))
                    scope_ok = False
                    scope_watermark = min(scope_watermark, complete_end)
                for gap_start, _gap_end in epoch_gaps:
                    if gap_start < effective_end:
                        scope_watermark = min(
                            scope_watermark,
                            max(effective_start, gap_start),
                        )
                        scope_ok = False
                if coverage_cursor is not None:
                    coverage_cursor = max(coverage_cursor, effective_end)
            if coverage_cursor is not None and coverage_cursor < to_ts:
                unknown.append((coverage_cursor, to_ts))
                scope_ok = False
                scope_watermark = min(scope_watermark, coverage_cursor)
            if not saw_effective_epoch:
                continue
            total += 1
            scope_through.append(scope_watermark)
            if is_storage_scope and scope_requirements:
                seen_storage_scopes.add(scope_id)
            if scope_ok:
                ok += 1

        live_required = live_start < to_ts
        if live_required:
            missing_storage_scopes = set(requirements_by_scope) - seen_storage_scopes
            for scope_id in sorted(missing_storage_scopes, key=str):
                requirement_start = max(
                    live_start,
                    min(
                        requirement.effective_from
                        for requirement in requirements_by_scope[scope_id]
                    ),
                )
                if requirement_start >= to_ts:
                    continue
                total += 1
                scope_through.append(requirement_start)
                unknown.append((requirement_start, to_ts))
            if missing_storage_authorities:
                # The process policy is the complete approved active source
                # set, not a loose allowlist. A configured source without its
                # immutable 0105 requirement set has no completeness proof.
                total += len(missing_storage_authorities)
                scope_through.extend(
                    live_start for _authority in missing_storage_authorities
                )
                unknown.append((live_start, to_ts))
        if live_required and total == 0:
            unknown.append((live_start, to_ts))

        merged_unknown = _merge_ranges(unknown, window_end=to_ts)
        data_candidates = [to_ts]
        if scope_through:
            data_candidates.append(min(scope_through))
        elif live_required:
            data_candidates.append(live_start)
        if merged_unknown:
            data_candidates.append(merged_unknown[0][0])
        data_through = min(max(min(data_candidates), coverage_start), to_ts)
        fully_post_cutover = from_ts >= snapshot.cutover_at
        live_complete = not live_required or (
            total > 0 and ok == total and min(scope_through) == to_ts
        )
        complete = (
            fully_post_cutover
            and rolled_complete
            and live_complete
            and not merged_unknown
            and data_through == to_ts
        )
        status = "complete" if complete else "partial"
        return _CoverageResult(
            status=status,
            data_through=data_through,
            required_sources_ok=ok,
            required_sources_total=total,
            unknown_ranges=merged_unknown,
        )


__all__ = [
    "AppUsageReadSnapshot",
    "SourceAwareUsageReadModel",
    "UsageDimensions",
    "UsageReadContractError",
    "UsageReadCutoverInactive",
]
