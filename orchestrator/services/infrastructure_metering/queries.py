"""Typed v2 compatibility summary over immutable point events.

Slice 0 intentionally exposes only legacy point-event semantics after the v2
daily model has bootstrapped and reconciled. Typed infrastructure intervals are
named as excluded coverage until Slice 1 can recompute overlap from source
capacity and integer microseconds and combine the daily model with a live tail.
Prorating their already-rounded stored quantity here would overstate precision.
"""

from __future__ import annotations

from collections.abc import Sequence
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import asyncpg

from .capabilities import MeteringSchemaCapabilities
from .materializer import StoragePublicationPolicy
from .read_model import SourceAwareUsageReadModel
from .types import (
    UsageCoverageV2,
    UsageRowV2,
    UsageSummaryV2,
    UsageWindowV2,
    decimal_text,
    ledger_cost,
)


@dataclass(frozen=True)
class UsageVisibility:
    owner_user_id: str | None = None
    visible_project_ids: tuple[str, ...] = ()
    scope_project_id: str | None = None
    include_non_customer: bool = False


_BASIS_SQL = """
COALESCE(measurement_basis, CASE
    WHEN category = 'llm' THEN 'api-consumed'
    WHEN category = 'compute' AND resource = 'workspace_pod'
        THEN 'scheduler-request'
    ELSE 'legacy-unknown'
END)
"""

_DOMAIN_SQL = """
COALESCE(cost_domain, CASE
    WHEN category = 'llm' THEN 'external-service'
    WHEN category = 'compute' AND resource = 'workspace_pod'
        THEN 'workload-allocation'
    ELSE 'unknown'
END)
"""

_CLASS_SQL = """
COALESCE(resource_class, CASE
    WHEN category = 'llm' THEN 'llm-model'
    WHEN category = 'compute' AND resource = 'workspace_pod'
        THEN 'kubernetes-pod'
    ELSE 'unknown'
END)
"""

_ATTRIBUTION_SQL = """
COALESCE(attribution_scope, CASE
    WHEN user_id IS NOT NULL OR project_id IS NOT NULL THEN 'customer'
    ELSE 'unknown'
END)
"""

_ALGORITHM_SQL = """
COALESCE(measurement_algorithm, CASE
    WHEN category = 'llm' THEN 'legacy-point-v1'
    WHEN category = 'compute' AND resource = 'workspace_pod'
        THEN 'legacy-end-stamped-v1'
    ELSE 'legacy-unknown-v1'
END)
"""

_QUERY = f"""
WITH normalized AS (
    SELECT
        category,
        resource,
        unit,
        {_BASIS_SQL} AS measurement_basis,
        {_DOMAIN_SQL} AS cost_domain,
        {_CLASS_SQL} AS resource_class,
        {_ATTRIBUTION_SQL} AS attribution_scope,
        {_ALGORITHM_SQL} AS measurement_algorithm,
        quantity AS window_quantity,
        cost_usd AS window_cost,
        cost_usd IS NOT NULL AS is_priced
    FROM usage_events
    WHERE period_start IS NULL AND ts >= $1 AND ts < $2
    {{visibility}}
)
SELECT
    category,
    resource,
    unit,
    measurement_basis,
    cost_domain,
    resource_class,
    attribution_scope,
    measurement_algorithm,
    SUM(window_quantity) AS quantity,
    SUM(window_cost) AS cost_usd,
    COALESCE(SUM(window_quantity) FILTER (WHERE is_priced), 0)
        AS priced_quantity,
    COALESCE(SUM(window_quantity) FILTER (WHERE NOT is_priced), 0)
        AS unpriced_quantity,
    COUNT(*) FILTER (WHERE is_priced) AS priced_events,
    COUNT(*) FILTER (WHERE NOT is_priced) AS unpriced_events,
    COUNT(*) AS events
FROM normalized
WHERE ($3::boolean OR attribution_scope = 'customer')
GROUP BY category, resource, unit, measurement_basis, cost_domain,
         resource_class, attribution_scope, measurement_algorithm
ORDER BY category, measurement_basis, resource_class, resource, unit,
         attribution_scope, cost_domain, measurement_algorithm
"""


def _as_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(str(value))


class UsageV2QueryService:
    def __init__(
        self,
        audit_pool: asyncpg.Pool | None,
        capabilities: MeteringSchemaCapabilities,
        app_pool: asyncpg.Pool | None = None,
        *,
        source_aware_reads_enabled: bool = False,
        enabled_resources: Sequence[str] = ("workspace_pod",),
        ide_workspace_pod_enabled: bool = False,
        storage_publication_policy: StoragePublicationPolicy | None = None,
    ):
        self._audit = audit_pool
        self._capabilities = capabilities
        self._source_aware_reads_enabled = source_aware_reads_enabled
        self._source_aware = (
            SourceAwareUsageReadModel(
                audit_pool,
                app_pool,
                enabled_resources=enabled_resources,
                ide_workspace_pod_enabled=ide_workspace_pod_enabled,
                storage_publication_policy=storage_publication_policy,
            )
            if source_aware_reads_enabled
            and audit_pool is not None
            and app_pool is not None
            else None
        )

    @property
    def is_available(self) -> bool:
        legacy_ready = self._audit is not None and self._capabilities.v2_reads_ready
        if not self._source_aware_reads_enabled:
            return legacy_ready
        return (
            legacy_ready
            and self._source_aware is not None
            and self._capabilities.slice1_runtime_ready
        )

    @property
    def source_aware_reads_enabled(self) -> bool:
        """Expose the selected path without weakening its schema readiness gate."""

        return self._source_aware_reads_enabled

    @property
    def capability_diagnostics(self) -> dict[str, Any]:
        return self._capabilities.diagnostics()

    async def summary(
        self,
        *,
        from_ts: datetime,
        to_ts: datetime,
        visibility: UsageVisibility,
        ref_id: str | None = None,
        as_of: datetime | None = None,
    ) -> UsageSummaryV2:
        if not self.is_available:
            raise RuntimeError("usage v2 schema is unavailable")
        if to_ts <= from_ts:
            raise ValueError("usage window end must be after its start")

        if self._source_aware_reads_enabled:
            assert self._source_aware is not None
            return await self._source_aware.summary(
                from_ts=from_ts,
                to_ts=to_ts,
                visibility=visibility,
                ref_id=ref_id,
                as_of=as_of,
            )

        clauses: list[str] = []
        params: list[Any] = [
            from_ts,
            to_ts,
            visibility.include_non_customer,
        ]
        # Identity visibility is authoritative. An MCP project scope narrows
        # that view; it never replaces it or revives revoked project access.
        if visibility.owner_user_id is not None:
            params.append(_as_uuid(visibility.owner_user_id))
            own = f"user_id = ${len(params)}"
            project_ids = [
                _as_uuid(project_id) for project_id in visibility.visible_project_ids
            ]
            if project_ids:
                params.append(project_ids)
                clauses.append(
                    f"AND ({own} OR project_id = ANY(${len(params)}::uuid[]))"
                )
            else:
                clauses.append(f"AND {own}")
        if visibility.scope_project_id is not None:
            params.append(_as_uuid(visibility.scope_project_id))
            clauses.append(f"AND project_id = ${len(params)}")
        if ref_id is not None:
            params.append(_as_uuid(ref_id))
            clauses.append(f"AND ref_id = ${len(params)}")

        sql = _QUERY.format(visibility="\n    ".join(clauses))
        rows = await self._audit.fetch(sql, *params)
        typed_rows = [self._row(row) for row in rows]
        observed_at = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
        return UsageSummaryV2(
            window=UsageWindowV2(
                start=from_ts,
                end=to_ts,
                as_of=observed_at,
                # Slice 0 has no source-completeness watermark for legacy point
                # materializers. Wall-clock time must not masquerade as one.
                data_through=None,
            ),
            rows=typed_rows,
            # Slice 0 exposes existing finalized ledger history but has no
            # authoritative resource inventories yet. Reporting partial is
            # intentional; absent Pod/VM/PVC rows must not look like zero use.
            coverage=UsageCoverageV2(
                status="partial",
                includes_provisional=False,
                required_sources_ok=0,
                required_sources_total=0,
                excluded_domains=[
                    "node-assets",
                    "idle",
                    "network",
                    "control-plane",
                    "live-resource-inventory",
                    "typed-infrastructure-intervals",
                ],
            ),
        )

    @staticmethod
    def _row(row: asyncpg.Record | dict[str, Any]) -> UsageRowV2:
        # Quantize the two mutually exclusive pricing buckets first, then derive
        # the total from their stored-precision values. Independently rounding
        # all three sums can differ by one 1e-18 quantum for legacy rows.
        priced_quantity = Decimal(decimal_text(row["priced_quantity"] or 0))
        unpriced_quantity = Decimal(decimal_text(row["unpriced_quantity"] or 0))
        quantity = priced_quantity + unpriced_quantity
        amount = None if row["cost_usd"] is None else decimal_text(row["cost_usd"])
        return UsageRowV2(
            category=str(row["category"]),
            measurement_basis=str(row["measurement_basis"]),
            cost_domain=str(row["cost_domain"]),
            resource_class=str(row["resource_class"]),
            measurement_algorithm=str(row["measurement_algorithm"]),
            resource=str(row["resource"]),
            unit=str(row["unit"]),
            attribution_scope=str(row["attribution_scope"]),
            quantity=decimal_text(quantity),
            finalized_quantity=decimal_text(quantity),
            confirmed_provisional_quantity="0",
            ledger_cost=ledger_cost(
                amount=amount,
                priced_quantity=priced_quantity,
                unpriced_quantity=unpriced_quantity,
                priced_events=int(row["priced_events"]),
                unpriced_events=int(row["unpriced_events"]),
            ),
            events=int(row["events"]),
        )


def visibility_from_kwargs(
    values: dict[str, Any], *, include_non_customer: bool = False
) -> UsageVisibility:
    return UsageVisibility(
        owner_user_id=values.get("owner_user_id"),
        visible_project_ids=tuple(values.get("visible_project_ids") or ()),
        scope_project_id=values.get("scope_project_id"),
        include_non_customer=include_non_customer,
    )


__all__ = ["UsageV2QueryService", "UsageVisibility", "visibility_from_kwargs"]
