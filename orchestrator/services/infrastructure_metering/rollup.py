"""Revision-driven typed daily usage rollup and bootstrap foundation.

This module owns the cross-database handoff from the immutable audit
``usage_events`` ledger to the app-local ``usage_daily_v2`` read model.  It is
deliberately isolated from startup and API reads until the Slice 0 rollout gate
is enabled.

The correctness boundary is split in two:

* one read-only, repeatable-read audit snapshot selects dirty revisions and
  aggregates each complete UTC day; and
* one app transaction claims a newer audit revision or a changed sealed
  coverage revision, full-replaces that day's rows, copies the exact coverage
  decision, and advances the watermark only when no unsealed infrastructure
  day would be crossed.

An audit insert racing the snapshot increments the dirty revision outside that
snapshot.  The app records only the revision it actually read, so the higher
revision remains eligible on the next bounded pass.  Concurrent rollup workers
are harmless because the app-side upsert is a stale-revision compare-and-set.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

import asyncpg

logger = logging.getLogger(__name__)

_WATERMARK = "usage_daily_v2"
_DEFAULT_LIMIT = 32
_MAX_LIMIT = 512
_SAFETY_LAG = timedelta(minutes=15)

# These expressions are the compatibility contract for immutable pre-v2 rows.
# Keep them equivalent to the temporary raw v2 query adapter until that query
# switches to this daily read model.
LEGACY_MEASUREMENT_BASIS_SQL = """
COALESCE(measurement_basis, CASE
    WHEN category = 'llm' THEN 'api-consumed'
    WHEN category = 'compute' AND resource = 'workspace_pod'
        THEN 'scheduler-request'
    ELSE 'legacy-unknown'
END)
"""

LEGACY_COST_DOMAIN_SQL = """
COALESCE(cost_domain, CASE
    WHEN category = 'llm' THEN 'external-service'
    WHEN category = 'compute' AND resource = 'workspace_pod'
        THEN 'workload-allocation'
    ELSE 'unknown'
END)
"""

LEGACY_RESOURCE_CLASS_SQL = """
COALESCE(resource_class, CASE
    WHEN category = 'llm' THEN 'llm-model'
    WHEN category = 'compute' AND resource = 'workspace_pod'
        THEN 'kubernetes-pod'
    ELSE 'unknown'
END)
"""

LEGACY_ATTRIBUTION_SCOPE_SQL = """
COALESCE(attribution_scope, CASE
    WHEN user_id IS NOT NULL OR project_id IS NOT NULL THEN 'customer'
    ELSE 'unknown'
END)
"""

LEGACY_MEASUREMENT_ALGORITHM_SQL = """
COALESCE(measurement_algorithm, CASE
    WHEN category = 'llm' THEN 'legacy-point-v1'
    WHEN category = 'compute' AND resource = 'workspace_pod'
        THEN 'legacy-end-stamped-v1'
    ELSE 'legacy-unknown-v1'
END)
"""

_APPLIED_REVISIONS_SQL = """
/* typed-rollup:applied-revisions */
SELECT day, applied_audit_revision
FROM usage_rollup_day_state
WHERE day <= $1
"""

_STALE_COVERAGE_REVISIONS_SQL = """
/* typed-rollup:stale-coverage-revisions */
SELECT rollup.day
FROM usage_rollup_day_state AS rollup
JOIN infra_usage_day_state AS infra ON infra.day = rollup.day
JOIN infra_metering_control AS control ON control.singleton = TRUE
WHERE rollup.day <= $1
  AND control.cutover_state IN ('preparing', 'active')
  AND control.cutover_at IS NOT NULL
  AND rollup.day >= (control.cutover_at AT TIME ZONE 'UTC')::date
  AND infra.state = 'sealed'
  AND rollup.infra_coverage_revision IS DISTINCT FROM infra.coverage_revision
ORDER BY rollup.day
LIMIT $2
"""

_MISSING_ROLLUP_DAYS_SQL = """
/* typed-rollup:missing-rollup-days */
SELECT infra.day
FROM infra_usage_day_state AS infra
JOIN infra_metering_control AS control ON control.singleton = TRUE
LEFT JOIN usage_rollup_day_state AS rollup ON rollup.day = infra.day
WHERE infra.day <= $1
  AND control.cutover_state IN ('preparing', 'active')
  AND control.cutover_at IS NOT NULL
  AND infra.day >= (control.cutover_at AT TIME ZONE 'UTC')::date
  AND infra.state = 'sealed'
  AND rollup.day IS NULL
ORDER BY infra.day
LIMIT $2
"""

_SEED_EVENT_FREE_DIRTY_SQL = """
/* typed-rollup:seed-event-free-dirty */
INSERT INTO usage_rollup_dirty_days (day, revision, updated_at)
SELECT candidate.day, 1, now()
FROM unnest($1::date[]) AS candidate(day)
ON CONFLICT (day) DO NOTHING
RETURNING day
"""

_PENDING_DIRTY_SQL = """
/* typed-rollup:pending-dirty */
WITH applied(day, revision) AS (
    SELECT * FROM unnest($1::date[], $2::bigint[])
)
SELECT dirty.day, dirty.revision
FROM usage_rollup_dirty_days AS dirty
LEFT JOIN applied USING (day)
WHERE dirty.day <= $3
  AND dirty.revision > COALESCE(applied.revision, 0)
ORDER BY dirty.day, dirty.revision
LIMIT $4
"""

_DIRTY_AFTER_SQL = """
/* typed-rollup:dirty-after */
SELECT day, revision
FROM usage_rollup_dirty_days
WHERE day <= $1 AND ($2::date IS NULL OR day > $2)
ORDER BY day
LIMIT $3
"""

_DAY_AGGREGATE_SQL = f"""
/* typed-rollup:day-aggregate */
WITH normalized AS (
    SELECT
        user_id,
        project_id,
        category,
        resource,
        unit,
        {LEGACY_MEASUREMENT_BASIS_SQL} AS measurement_basis,
        {LEGACY_RESOURCE_CLASS_SQL} AS resource_class,
        {LEGACY_ATTRIBUTION_SCOPE_SQL} AS attribution_scope,
        {LEGACY_COST_DOMAIN_SQL} AS cost_domain,
        {LEGACY_MEASUREMENT_ALGORITHM_SQL} AS measurement_algorithm,
        quantity,
        cost_usd,
        source IN (
            'infra-allocation-v2',
            'infra-allocation-correction-v2'
        ) AS is_infrastructure_v2
    FROM usage_events
    WHERE ts >= $1 AND ts < $2
), grouped AS (
SELECT
    user_id,
    project_id,
    category,
    resource,
    unit,
    measurement_basis,
    resource_class,
    attribution_scope,
    cost_domain,
    measurement_algorithm,
    COALESCE(
        SUM(quantity) FILTER (WHERE cost_usd IS NOT NULL),
        0::numeric
    ) AS raw_priced_quantity,
    COALESCE(
        SUM(quantity) FILTER (WHERE cost_usd IS NULL),
        0::numeric
    ) AS raw_unpriced_quantity,
    SUM(cost_usd) AS raw_cost_usd,
    COUNT(*) FILTER (WHERE cost_usd IS NOT NULL) AS priced_events,
    COUNT(*) FILTER (WHERE cost_usd IS NULL) AS unpriced_events,
    COUNT(*) AS events,
    BOOL_OR(is_infrastructure_v2) AS has_infrastructure_v2
FROM normalized
GROUP BY
    user_id, project_id, category, resource, unit, measurement_basis,
    resource_class, attribution_scope, cost_domain, measurement_algorithm
), rounded AS (
SELECT
    user_id,
    project_id,
    category,
    resource,
    unit,
    measurement_basis,
    resource_class,
    attribution_scope,
    cost_domain,
    measurement_algorithm,
    public.round_half_even_v2(raw_priced_quantity, 18)
        AS priced_quantity,
    public.round_half_even_v2(raw_unpriced_quantity, 18)
        AS unpriced_quantity,
    CASE
        WHEN priced_events = 0 THEN NULL
        ELSE public.round_half_even_v2(raw_cost_usd, 18)
    END AS cost_usd,
    priced_events,
    unpriced_events,
    events,
    has_infrastructure_v2
FROM grouped
)
SELECT
    user_id,
    project_id,
    category,
    resource,
    unit,
    measurement_basis,
    resource_class,
    attribution_scope,
    cost_domain,
    measurement_algorithm,
    priced_quantity + unpriced_quantity AS quantity,
    cost_usd,
    priced_quantity,
    unpriced_quantity,
    priced_events,
    unpriced_events,
    events,
    has_infrastructure_v2
FROM rounded
ORDER BY
    category, measurement_basis, resource_class, resource, unit,
    attribution_scope, cost_domain, measurement_algorithm, user_id, project_id
"""

_DAY_COVERAGE_SQL = """
/* typed-rollup:day-coverage */
SELECT state, coverage_status, coverage_revision, unknown_ranges
FROM infra_usage_day_state
WHERE day = $1
FOR SHARE
"""

_CAS_DAY_STATE_SQL = """
/* typed-rollup:cas-day-state */
INSERT INTO usage_rollup_day_state (
    day, applied_audit_revision, coverage_status, unknown_ranges,
    infra_coverage_revision,
    rolled_at, updated_at
)
VALUES ($1, $2, $3, $4::jsonb, $5, now(), now())
ON CONFLICT (day) DO UPDATE SET
    applied_audit_revision = EXCLUDED.applied_audit_revision,
    coverage_status = EXCLUDED.coverage_status,
    unknown_ranges = EXCLUDED.unknown_ranges,
    infra_coverage_revision = EXCLUDED.infra_coverage_revision,
    rolled_at = EXCLUDED.rolled_at,
    updated_at = now()
WHERE usage_rollup_day_state.applied_audit_revision
          < EXCLUDED.applied_audit_revision
   OR (
        usage_rollup_day_state.applied_audit_revision
            = EXCLUDED.applied_audit_revision
        AND usage_rollup_day_state.infra_coverage_revision
            IS DISTINCT FROM EXCLUDED.infra_coverage_revision
   )
RETURNING applied_audit_revision
"""

_DELETE_DAY_SQL = """
/* typed-rollup:delete-day */
DELETE FROM usage_daily_v2 WHERE day = $1
"""

_INSERT_DAY_ROW_SQL = """
/* typed-rollup:insert-day-row */
INSERT INTO usage_daily_v2 (
    day, user_id, project_id, category, resource, unit,
    measurement_basis, resource_class, attribution_scope, cost_domain,
    measurement_algorithm, quantity, cost_usd, priced_quantity,
    unpriced_quantity, priced_events, unpriced_events, events, updated_at
)
VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14, $15, $16, $17, $18, now()
)
"""

_WATERMARK_FOR_UPDATE_SQL = """
/* typed-rollup:watermark-for-update */
SELECT last_closed_day
FROM rollup_state
WHERE name = $1
FOR UPDATE
"""

_FIRST_UNSEALED_SQL = """
/* typed-rollup:first-unsealed */
WITH cutover AS (
    SELECT (cutover_at AT TIME ZONE 'UTC')::date AS first_infra_day
    FROM infra_metering_control
    WHERE singleton = TRUE
      AND cutover_state IN ('preparing', 'active')
      AND cutover_at IS NOT NULL
), required_days AS (
    SELECT generated.day::date
    FROM cutover
    CROSS JOIN LATERAL generate_series(
        GREATEST(
            cutover.first_infra_day,
            COALESCE($1::date + 1, cutover.first_infra_day)
        ),
        $2::date,
        interval '1 day'
    ) AS generated(day)
), blocked AS (
    SELECT state.day
    FROM infra_usage_day_state AS state
    WHERE ($1::date IS NULL OR state.day > $1)
      AND state.day <= $2
      AND state.state <> 'sealed'
    UNION
    SELECT required.day
    FROM required_days AS required
    LEFT JOIN infra_usage_day_state AS state USING (day)
    WHERE state.day IS NULL OR state.state <> 'sealed'
)
SELECT day
FROM blocked
ORDER BY day
LIMIT 1
"""

_ADVANCE_WATERMARK_SQL = """
/* typed-rollup:advance-watermark */
UPDATE rollup_state
SET last_closed_day = $1, updated_at = now()
WHERE name = $2
  AND (last_closed_day IS NULL OR last_closed_day < $1)
RETURNING last_closed_day
"""

_BOOTSTRAP_STATE_SQL = """
/* typed-rollup:bootstrap-state */
SELECT status, seeded_through_day, reconciled_through_day,
       started_at, completed_at, sanitized_error,
       watermark.last_closed_day
FROM usage_rollup_v2_bootstrap_state AS bootstrap
LEFT JOIN rollup_state AS watermark ON watermark.name = 'usage_daily_v2'
WHERE bootstrap.singleton = TRUE
"""

_BOOTSTRAP_START_SQL = """
/* typed-rollup:bootstrap-start */
UPDATE usage_rollup_v2_bootstrap_state
SET status = 'running',
    seeded_through_day = NULL,
    reconciled_through_day = NULL,
    started_at = COALESCE(started_at, now()),
    completed_at = NULL,
    sanitized_error = NULL,
    updated_at = now()
WHERE singleton = TRUE
  AND status IN ('pending', 'error')
RETURNING status
"""

_SEED_DIRTY_SQL = """
/* typed-rollup:seed-dirty */
WITH retained_days AS (
    SELECT DISTINCT (ts AT TIME ZONE 'UTC')::date AS day
    FROM usage_events
    WHERE ts < $1
), seeded AS (
    INSERT INTO usage_rollup_dirty_days (day, revision, updated_at)
    SELECT day, 1, now()
    FROM retained_days
    ON CONFLICT (day) DO NOTHING
    RETURNING day
)
SELECT COUNT(*) AS inserted_days,
       (SELECT MAX(day) FROM retained_days) AS last_retained_day
FROM seeded
"""

_BOOTSTRAP_SEEDED_SQL = """
/* typed-rollup:bootstrap-seeded */
UPDATE usage_rollup_v2_bootstrap_state
SET status = 'reconciling',
    seeded_through_day = $1,
    reconciled_through_day = NULL,
    completed_at = NULL,
    sanitized_error = NULL,
    updated_at = now()
WHERE singleton = TRUE
  AND status = 'running'
  AND seeded_through_day IS NULL
RETURNING seeded_through_day
"""

_APP_DAY_STATE_SQL = """
/* typed-rollup:app-day-state */
SELECT applied_audit_revision
FROM usage_rollup_day_state
WHERE day = $1
"""

_APP_DAY_ROWS_SQL = """
/* typed-rollup:app-day-rows */
SELECT
    user_id, project_id, category, resource, unit, measurement_basis,
    resource_class, attribution_scope, cost_domain, measurement_algorithm,
    quantity, cost_usd, priced_quantity, unpriced_quantity,
    priced_events, unpriced_events, events
FROM usage_daily_v2
WHERE day = $1
ORDER BY
    category, measurement_basis, resource_class, resource, unit,
    attribution_scope, cost_domain, measurement_algorithm, user_id, project_id
"""

_BOOTSTRAP_RECONCILED_SQL = """
/* typed-rollup:bootstrap-reconciled */
UPDATE usage_rollup_v2_bootstrap_state
SET reconciled_through_day = $1, updated_at = now()
WHERE singleton = TRUE
  AND status = 'reconciling'
  AND reconciled_through_day IS NOT DISTINCT FROM $2
RETURNING reconciled_through_day
"""

_BOOTSTRAP_COMPLETE_SQL = """
/* typed-rollup:bootstrap-complete */
UPDATE usage_rollup_v2_bootstrap_state
SET status = 'complete',
    reconciled_through_day = seeded_through_day,
    completed_at = now(),
    sanitized_error = NULL,
    updated_at = now()
WHERE singleton = TRUE
  AND status = 'reconciling'
  AND seeded_through_day = $1
RETURNING status
"""

_BOOTSTRAP_ERROR_SQL = """
/* typed-rollup:bootstrap-error */
UPDATE usage_rollup_v2_bootstrap_state
SET status = 'error', sanitized_error = $1::jsonb, updated_at = now()
WHERE singleton = TRUE AND status <> 'complete'
"""


class RollupContractError(ValueError):
    """Raised when a database row violates the typed rollup contract."""


class ApplyDisposition(StrEnum):
    APPLIED = "applied"
    STALE = "stale"
    UNSEALED = "unsealed"


class BootstrapStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RECONCILING = "reconciling"
    COMPLETE = "complete"
    ERROR = "error"


def _decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise RollupContractError(f"{field_name} must be an exact decimal")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RollupContractError(f"{field_name} must be an exact decimal") from exc
    if not parsed.is_finite():
        raise RollupContractError(f"{field_name} must be finite")
    return Decimal(0) if parsed.is_zero() else parsed


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    numbers = tuple(values)
    if not numbers:
        return Decimal(0)
    exponent = min(int(number.as_tuple().exponent) for number in numbers)
    total = 0
    for number in numbers:
        parts = number.as_tuple()
        coefficient = 0
        for digit in parts.digits:
            coefficient = coefficient * 10 + digit
        if parts.sign:
            coefficient = -coefficient
        total += coefficient * (10 ** (int(parts.exponent) - exponent))
    if total == 0:
        return Decimal(0)
    return Decimal(
        (
            int(total < 0),
            tuple(int(digit) for digit in str(abs(total))),
            exponent,
        )
    )


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RollupContractError(f"{field_name} must be a non-empty string")
    return value


def _event_count(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RollupContractError(f"{field_name} must be a non-negative integer")
    return value


def legacy_dimensions(row: Mapping[str, Any]) -> dict[str, str]:
    """Return the source-specific dimensions used for immutable legacy rows."""

    category = _required_text(row.get("category"), "category")
    resource = _required_text(row.get("resource"), "resource")
    owner_known = row.get("user_id") is not None or row.get("project_id") is not None
    workspace = category == "compute" and resource == "workspace_pod"

    def supplied_or(field_name: str, fallback: str) -> str:
        value = row.get(field_name)
        return fallback if value is None else _required_text(value, field_name)

    return {
        "measurement_basis": supplied_or(
            "measurement_basis",
            (
                "api-consumed"
                if category == "llm"
                else "scheduler-request"
                if workspace
                else "legacy-unknown"
            ),
        ),
        "cost_domain": supplied_or(
            "cost_domain",
            (
                "external-service"
                if category == "llm"
                else "workload-allocation"
                if workspace
                else "unknown"
            ),
        ),
        "resource_class": supplied_or(
            "resource_class",
            (
                "llm-model"
                if category == "llm"
                else "kubernetes-pod"
                if workspace
                else "unknown"
            ),
        ),
        "attribution_scope": supplied_or(
            "attribution_scope", "customer" if owner_known else "unknown"
        ),
        "measurement_algorithm": supplied_or(
            "measurement_algorithm",
            (
                "legacy-point-v1"
                if category == "llm"
                else "legacy-end-stamped-v1"
                if workspace
                else "legacy-unknown-v1"
            ),
        ),
    }


@dataclass(frozen=True, slots=True)
class DailyUsageDimensions:
    user_id: Any | None
    project_id: Any | None
    category: str
    resource: str
    unit: str
    measurement_basis: str
    resource_class: str
    attribution_scope: str
    cost_domain: str
    measurement_algorithm: str

    def __post_init__(self) -> None:
        for field_name in (
            "category",
            "resource",
            "unit",
            "measurement_basis",
            "resource_class",
            "attribution_scope",
            "cost_domain",
            "measurement_algorithm",
        ):
            _required_text(getattr(self, field_name), field_name)
        if self.attribution_scope not in {"customer", "shared-platform", "unknown"}:
            raise RollupContractError("invalid attribution_scope")

    def key(self) -> tuple[Any, ...]:
        return (
            self.user_id,
            self.project_id,
            self.category,
            self.resource,
            self.unit,
            self.measurement_basis,
            self.resource_class,
            self.attribution_scope,
            self.cost_domain,
            self.measurement_algorithm,
        )


@dataclass(frozen=True, slots=True)
class DailyUsageRow:
    dimensions: DailyUsageDimensions
    quantity: Decimal
    cost_usd: Decimal | None
    priced_quantity: Decimal
    unpriced_quantity: Decimal
    priced_events: int
    unpriced_events: int
    events: int

    def __post_init__(self) -> None:
        for field_name in ("quantity", "priced_quantity", "unpriced_quantity"):
            object.__setattr__(
                self, field_name, _decimal(getattr(self, field_name), field_name)
            )
        if self.cost_usd is not None:
            object.__setattr__(self, "cost_usd", _decimal(self.cost_usd, "cost_usd"))
        for field_name in ("priced_events", "unpriced_events", "events"):
            object.__setattr__(
                self, field_name, _event_count(getattr(self, field_name), field_name)
            )
        if self.events != self.priced_events + self.unpriced_events:
            raise RollupContractError(
                "events must equal priced_events plus unpriced_events"
            )
        if self.quantity != _exact_sum((self.priced_quantity, self.unpriced_quantity)):
            raise RollupContractError(
                "quantity must equal priced_quantity plus unpriced_quantity"
            )
        if (self.priced_events == 0) != (self.cost_usd is None):
            raise RollupContractError(
                "cost_usd must be null exactly when priced_events is zero"
            )

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> DailyUsageRow:
        dimensions = DailyUsageDimensions(
            user_id=row.get("user_id"),
            project_id=row.get("project_id"),
            category=_required_text(row["category"], "category"),
            resource=_required_text(row["resource"], "resource"),
            unit=_required_text(row["unit"], "unit"),
            measurement_basis=_required_text(
                row["measurement_basis"], "measurement_basis"
            ),
            resource_class=_required_text(row["resource_class"], "resource_class"),
            attribution_scope=_required_text(
                row["attribution_scope"], "attribution_scope"
            ),
            cost_domain=_required_text(row["cost_domain"], "cost_domain"),
            measurement_algorithm=_required_text(
                row["measurement_algorithm"], "measurement_algorithm"
            ),
        )
        return cls(
            dimensions=dimensions,
            quantity=row["quantity"],
            cost_usd=row.get("cost_usd"),
            priced_quantity=row["priced_quantity"],
            unpriced_quantity=row["unpriced_quantity"],
            priced_events=row["priced_events"],
            unpriced_events=row["unpriced_events"],
            events=row["events"],
        )

    def db_values(self, day: date) -> tuple[Any, ...]:
        dimensions = self.dimensions
        return (
            day,
            dimensions.user_id,
            dimensions.project_id,
            dimensions.category,
            dimensions.resource,
            dimensions.unit,
            dimensions.measurement_basis,
            dimensions.resource_class,
            dimensions.attribution_scope,
            dimensions.cost_domain,
            dimensions.measurement_algorithm,
            self.quantity,
            self.cost_usd,
            self.priced_quantity,
            self.unpriced_quantity,
            self.priced_events,
            self.unpriced_events,
            self.events,
        )

    def comparison_key(self) -> tuple[Any, ...]:
        return self.dimensions.key() + (
            self.quantity,
            self.cost_usd,
            self.priced_quantity,
            self.unpriced_quantity,
            self.priced_events,
            self.unpriced_events,
            self.events,
        )


@dataclass(frozen=True, slots=True)
class AuditDaySnapshot:
    day: date
    revision: int
    rows: tuple[DailyUsageRow, ...]
    requires_infrastructure_seal: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision <= 0
        ):
            raise RollupContractError("audit revision must be a positive integer")
        object.__setattr__(self, "rows", tuple(self.rows))
        keys = [row.dimensions.key() for row in self.rows]
        if len(keys) != len(set(keys)):
            raise RollupContractError("daily snapshot contains duplicate dimensions")


@dataclass(frozen=True, slots=True)
class DayApplyResult:
    day: date
    revision: int
    disposition: ApplyDisposition
    rows: int = 0
    watermark: date | None = None
    blocked_day: date | None = None


@dataclass(frozen=True, slots=True)
class RollupPassResult:
    available: bool
    selected: int = 0
    applied: int = 0
    stale: int = 0
    rows: int = 0
    blocked_day: date | None = None
    watermark: date | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class BootstrapState:
    status: BootstrapStatus
    seeded_through_day: date | None = None
    reconciled_through_day: date | None = None
    watermark: date | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    sanitized_error: Mapping[str, Any] | None = None

    @property
    def read_ready(self) -> bool:
        """Return true only for a durably completed, fully published bootstrap."""

        return (
            self.status is BootstrapStatus.COMPLETE
            and self.seeded_through_day is not None
            and self.reconciled_through_day == self.seeded_through_day
            and self.completed_at is not None
            and self.watermark is not None
            and self.watermark >= self.seeded_through_day
        )

    @classmethod
    def from_record(cls, row: Mapping[str, Any]) -> BootstrapState:
        error = row.get("sanitized_error")
        if isinstance(error, str):
            error = json.loads(error)
        return cls(
            status=BootstrapStatus(row["status"]),
            seeded_through_day=row.get("seeded_through_day"),
            reconciled_through_day=row.get("reconciled_through_day"),
            watermark=row.get("last_closed_day"),
            started_at=row.get("started_at"),
            completed_at=row.get("completed_at"),
            sanitized_error=error,
        )


@dataclass(frozen=True, slots=True)
class BootstrapStepResult:
    available: bool
    status: BootstrapStatus
    seeded_through_day: date | None = None
    reconciled_through_day: date | None = None
    rollup: RollupPassResult | None = None
    reconciled_days: int = 0
    blocked_day: date | None = None
    error_code: str | None = None


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _closeable_day(now: datetime) -> date:
    if now.tzinfo is None or now.utcoffset() is None:
        raise RollupContractError("now must be timezone-aware")
    return (now.astimezone(timezone.utc) - _SAFETY_LAG).date() - timedelta(days=1)


def _bounded_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise RollupContractError("limit must be a positive integer")
    return min(limit, _MAX_LIMIT)


def _json_array(value: Any) -> list[Any]:
    if value is None:
        return []
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list):
        raise RollupContractError("unknown_ranges must be a JSON array")
    return parsed


class TypedUsageDailyRollup:
    """Bounded, revision-driven writer for ``usage_daily_v2``."""

    def __init__(
        self,
        audit_pool: asyncpg.Pool | None,
        app_pool: asyncpg.Pool | None,
    ) -> None:
        self._audit = audit_pool
        self._app = app_pool

    @property
    def is_available(self) -> bool:
        return self._audit is not None and self._app is not None

    async def bootstrap_state(self) -> BootstrapState:
        if self._app is None:
            return BootstrapState(BootstrapStatus.PENDING)
        row = await self._app.fetchrow(_BOOTSTRAP_STATE_SQL)
        if row is None:
            return BootstrapState(BootstrapStatus.PENDING)
        return BootstrapState.from_record(row)

    async def run_cycle(
        self,
        *,
        now: datetime | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> RollupPassResult | BootstrapStepResult:
        """Advance bootstrap until complete, then run the ordinary dirty pass."""

        if not self.is_available:
            return RollupPassResult(available=False)
        try:
            state = await self.bootstrap_state()
            if state.status is not BootstrapStatus.COMPLETE:
                return await self.run_bootstrap_step(now=now, limit=limit)
            if not state.read_ready:
                raise RollupContractError(
                    "completed bootstrap does not satisfy the durable readiness contract"
                )
            return await self.run_pass(now=now, limit=limit)
        except Exception as exc:
            logger.warning("typed usage rollup cycle failed (non-fatal)", exc_info=True)
            return RollupPassResult(
                available=True,
                error_code=f"rollup-cycle-{type(exc).__name__}",
            )

    async def run_pass(
        self,
        *,
        now: datetime | None = None,
        through_day: date | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> RollupPassResult:
        """Apply at most ``limit`` dirty days, oldest outstanding day first."""

        if not self.is_available:
            return RollupPassResult(available=False)
        limit = _bounded_limit(limit)
        target = through_day or _closeable_day(now or datetime.now(timezone.utc))
        try:
            await self._seed_missing_rollup_days(target, limit)
            applied_revisions = await self._applied_revisions(target)
            coverage_refresh_days = await self._stale_coverage_revision_days(
                target, limit
            )
            snapshots = await self._read_pending_snapshots(
                applied_revisions,
                target,
                limit,
                coverage_refresh_days=coverage_refresh_days,
            )
            applied = stale = rows = 0
            watermark: date | None = None
            for snapshot in snapshots:
                result = await self._apply_snapshot(snapshot)
                watermark = result.watermark or watermark
                if result.disposition is ApplyDisposition.UNSEALED:
                    return RollupPassResult(
                        available=True,
                        selected=len(snapshots),
                        applied=applied,
                        stale=stale,
                        rows=rows,
                        blocked_day=result.blocked_day or snapshot.day,
                        watermark=watermark,
                    )
                if result.disposition is ApplyDisposition.STALE:
                    stale += 1
                else:
                    applied += 1
                    rows += result.rows
                    if result.blocked_day is not None:
                        return RollupPassResult(
                            available=True,
                            selected=len(snapshots),
                            applied=applied,
                            stale=stale,
                            rows=rows,
                            blocked_day=result.blocked_day,
                            watermark=watermark,
                        )
            # Once every dirty revision through the close boundary is applied,
            # close event-free days too. The cutover-aware seal query below is
            # what makes this empty-ledger shortcut safe.
            if not await self._has_pending_work(target):
                blocked_day, advanced = await self._advance_watermark_through(target)
                watermark = advanced or watermark
                if blocked_day is not None:
                    return RollupPassResult(
                        available=True,
                        selected=len(snapshots),
                        applied=applied,
                        stale=stale,
                        rows=rows,
                        blocked_day=blocked_day,
                        watermark=watermark,
                    )
            return RollupPassResult(
                available=True,
                selected=len(snapshots),
                applied=applied,
                stale=stale,
                rows=rows,
                watermark=watermark,
            )
        except Exception as exc:
            logger.warning("typed usage rollup pass failed (non-fatal)", exc_info=True)
            return RollupPassResult(
                available=True,
                error_code=f"rollup-pass-{type(exc).__name__}",
            )

    async def run_bootstrap_step(
        self,
        *,
        now: datetime | None = None,
        through_day: date | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> BootstrapStepResult:
        """Run one bounded, crash-resumable bootstrap/reconciliation step."""

        if not self.is_available:
            return BootstrapStepResult(False, BootstrapStatus.PENDING)
        limit = _bounded_limit(limit)
        target = through_day or _closeable_day(now or datetime.now(timezone.utc))
        try:
            state = await self.bootstrap_state()
            if state.status is BootstrapStatus.COMPLETE:
                return BootstrapStepResult(
                    True,
                    state.status,
                    state.seeded_through_day,
                    state.reconciled_through_day,
                )
            if state.status in {
                BootstrapStatus.PENDING,
                BootstrapStatus.ERROR,
            } or (
                state.status is BootstrapStatus.RUNNING
                and state.seeded_through_day is None
            ):
                await self._seed_bootstrap(target)
                state = await self.bootstrap_state()

            seeded_through = state.seeded_through_day
            if seeded_through is None:
                raise RollupContractError("bootstrap seed boundary is missing")

            rollup_result = await self.run_pass(through_day=seeded_through, limit=limit)
            if rollup_result.error_code is not None:
                raise RuntimeError(rollup_result.error_code)
            if rollup_result.blocked_day is not None:
                return BootstrapStepResult(
                    True,
                    BootstrapStatus.RECONCILING,
                    seeded_through,
                    state.reconciled_through_day,
                    rollup=rollup_result,
                    blocked_day=rollup_result.blocked_day,
                )
            if await self._has_pending_work(seeded_through):
                return BootstrapStepResult(
                    True,
                    BootstrapStatus.RECONCILING,
                    seeded_through,
                    state.reconciled_through_day,
                    rollup=rollup_result,
                )

            reconciled, cursor, mismatch = await self._reconcile_batch(
                seeded_through,
                state.reconciled_through_day,
                limit,
            )
            if mismatch:
                return BootstrapStepResult(
                    True,
                    BootstrapStatus.RECONCILING,
                    seeded_through,
                    cursor,
                    rollup=rollup_result,
                    reconciled_days=reconciled,
                    error_code="bootstrap-reconciliation-mismatch",
                )

            more_to_reconcile = await self._has_dirty_after(seeded_through, cursor)
            if more_to_reconcile:
                return BootstrapStepResult(
                    True,
                    BootstrapStatus.RECONCILING,
                    seeded_through,
                    cursor,
                    rollup=rollup_result,
                    reconciled_days=reconciled,
                )

            # A final revision comparison closes the race between the earlier
            # rollup pass and reconciliation. A later insert remains ordinary
            # dirty-day work after bootstrap, exactly like any other late row.
            if await self._has_pending_work(seeded_through):
                return BootstrapStepResult(
                    True,
                    BootstrapStatus.RECONCILING,
                    seeded_through,
                    cursor,
                    rollup=rollup_result,
                    reconciled_days=reconciled,
                )

            blocked_day, watermark = await self._advance_watermark_through(
                seeded_through
            )
            if blocked_day is not None:
                return BootstrapStepResult(
                    True,
                    BootstrapStatus.RECONCILING,
                    seeded_through,
                    cursor,
                    rollup=rollup_result,
                    reconciled_days=reconciled,
                    blocked_day=blocked_day,
                )
            await self._complete_bootstrap(seeded_through)
            return BootstrapStepResult(
                True,
                BootstrapStatus.COMPLETE,
                seeded_through,
                seeded_through,
                rollup=replace(
                    rollup_result,
                    watermark=watermark or rollup_result.watermark,
                ),
                reconciled_days=reconciled,
            )
        except Exception as exc:
            logger.warning("typed usage bootstrap failed (non-fatal)", exc_info=True)
            await self._record_bootstrap_error(exc)
            return BootstrapStepResult(
                True,
                BootstrapStatus.ERROR,
                error_code=f"bootstrap-{type(exc).__name__}",
            )

    async def _applied_revisions(self, through_day: date) -> dict[date, int]:
        assert self._app is not None
        rows = await self._app.fetch(_APPLIED_REVISIONS_SQL, through_day)
        return {row["day"]: int(row["applied_audit_revision"]) for row in rows}

    async def _seed_missing_rollup_days(self, through_day: date, limit: int) -> None:
        """Give sealed event-free days a positive, replay-safe audit revision."""

        assert self._app is not None and self._audit is not None
        rows = await self._app.fetch(
            _MISSING_ROLLUP_DAYS_SQL,
            through_day,
            _bounded_limit(limit),
        )
        days: list[date] = []
        for row in rows:
            day = row["day"]
            if not isinstance(day, date) or isinstance(day, datetime):
                raise RollupContractError("missing rollup day is invalid")
            if days and day <= days[-1]:
                raise RollupContractError(
                    "missing rollup days are not strictly ordered"
                )
            days.append(day)
        if not days:
            return
        async with self._audit.acquire() as conn:
            await conn.fetch(_SEED_EVENT_FREE_DIRTY_SQL, days)

    async def _stale_coverage_revision_days(
        self, through_day: date, limit: int
    ) -> tuple[date, ...]:
        assert self._app is not None
        rows = await self._app.fetch(
            _STALE_COVERAGE_REVISIONS_SQL,
            through_day,
            _bounded_limit(limit),
        )
        days: list[date] = []
        for row in rows:
            day = row["day"]
            if not isinstance(day, date):
                raise RollupContractError("stale coverage revision has invalid day")
            if days and day <= days[-1]:
                raise RollupContractError(
                    "stale coverage revision days are not strictly ordered"
                )
            days.append(day)
        return tuple(days)

    async def _read_pending_snapshots(
        self,
        applied_revisions: Mapping[date, int],
        through_day: date,
        limit: int,
        *,
        coverage_refresh_days: Sequence[date] = (),
    ) -> tuple[AuditDaySnapshot, ...]:
        assert self._audit is not None
        items = sorted(applied_revisions.items())
        days = [item[0] for item in items]
        revisions = [item[1] for item in items]
        async with self._audit.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                dirty = await conn.fetch(
                    _PENDING_DIRTY_SQL,
                    days,
                    revisions,
                    through_day,
                    limit,
                )
                selected: dict[date, int] = {
                    item["day"]: int(item["revision"]) for item in dirty
                }
                for day in coverage_refresh_days:
                    if len(selected) >= limit:
                        break
                    if day in selected:
                        continue
                    current = await conn.fetchrow(
                        "/* typed-rollup:dirty-revision */ "
                        "SELECT revision FROM usage_rollup_dirty_days WHERE day = $1",
                        day,
                    )
                    if current is None:
                        raise RollupContractError(
                            "coverage refresh has no retained audit revision"
                        )
                    revision = int(current["revision"])
                    applied = applied_revisions.get(day)
                    if applied is None or revision < applied:
                        raise RollupContractError(
                            "coverage refresh audit revision precedes app state"
                        )
                    selected[day] = revision

                snapshots = []
                for day, revision in sorted(selected.items()):
                    snapshots.append(
                        await self._read_snapshot_in_transaction(
                            conn,
                            day,
                            revision,
                        )
                    )
                return tuple(snapshots)

    async def _read_snapshot(self, day: date, revision: int) -> AuditDaySnapshot:
        """Read one explicit revision and aggregate in one audit snapshot."""

        assert self._audit is not None
        async with self._audit.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                current = await conn.fetchrow(
                    "/* typed-rollup:dirty-revision */ "
                    "SELECT revision FROM usage_rollup_dirty_days WHERE day = $1",
                    day,
                )
                if current is None or int(current["revision"]) != revision:
                    raise RollupContractError("requested audit revision is stale")
                return await self._read_snapshot_in_transaction(conn, day, revision)

    async def _read_snapshot_in_transaction(
        self, conn: asyncpg.Connection, day: date, revision: int
    ) -> AuditDaySnapshot:
        raw_rows = await conn.fetch(
            _DAY_AGGREGATE_SQL,
            _midnight(day),
            _midnight(day + timedelta(days=1)),
        )
        rows = tuple(DailyUsageRow.from_record(row) for row in raw_rows)
        requires_seal = any(bool(row.get("has_infrastructure_v2")) for row in raw_rows)
        return AuditDaySnapshot(day, revision, rows, requires_seal)

    async def _apply_snapshot(self, snapshot: AuditDaySnapshot) -> DayApplyResult:
        assert self._app is not None
        async with self._app.acquire() as conn:
            async with conn.transaction():
                coverage_row = await conn.fetchrow(_DAY_COVERAGE_SQL, snapshot.day)
                if coverage_row is not None and coverage_row["state"] != "sealed":
                    return DayApplyResult(
                        snapshot.day,
                        snapshot.revision,
                        ApplyDisposition.UNSEALED,
                        blocked_day=snapshot.day,
                    )
                if coverage_row is None and snapshot.requires_infrastructure_seal:
                    return DayApplyResult(
                        snapshot.day,
                        snapshot.revision,
                        ApplyDisposition.UNSEALED,
                        blocked_day=snapshot.day,
                    )

                coverage_status = (
                    str(coverage_row["coverage_status"])
                    if coverage_row is not None
                    else "complete"
                )
                unknown_ranges = (
                    _json_array(coverage_row["unknown_ranges"])
                    if coverage_row is not None
                    else []
                )
                infra_coverage_revision: str | None = None
                if coverage_row is not None:
                    raw_revision = coverage_row["coverage_revision"]
                    if not isinstance(raw_revision, str) or not raw_revision:
                        raise RollupContractError(
                            "sealed infrastructure day has no coverage revision"
                        )
                    infra_coverage_revision = raw_revision
                claimed = await conn.fetchval(
                    _CAS_DAY_STATE_SQL,
                    snapshot.day,
                    snapshot.revision,
                    coverage_status,
                    json.dumps(unknown_ranges, separators=(",", ":")),
                    infra_coverage_revision,
                )
                if claimed is None:
                    return DayApplyResult(
                        snapshot.day,
                        snapshot.revision,
                        ApplyDisposition.STALE,
                    )

                await conn.execute(_DELETE_DAY_SQL, snapshot.day)
                if snapshot.rows:
                    await conn.executemany(
                        _INSERT_DAY_ROW_SQL,
                        [row.db_values(snapshot.day) for row in snapshot.rows],
                    )
                blocked_day, watermark = await self._advance_watermark_in_transaction(
                    conn, snapshot.day
                )
                # Applying a historical dirty revision is still valid even when
                # an earlier unsealed day prevents forward watermark movement.
                return DayApplyResult(
                    snapshot.day,
                    snapshot.revision,
                    ApplyDisposition.APPLIED,
                    rows=len(snapshot.rows),
                    watermark=watermark,
                    blocked_day=blocked_day,
                )

    async def _advance_watermark_in_transaction(
        self, conn: asyncpg.Connection, through_day: date
    ) -> tuple[date | None, date | None]:
        row = await conn.fetchrow(_WATERMARK_FOR_UPDATE_SQL, _WATERMARK)
        if row is None:
            raise RollupContractError("typed usage rollup watermark row is missing")
        current = row["last_closed_day"]
        if current is not None and current >= through_day:
            return None, current
        blocked = await conn.fetchval(_FIRST_UNSEALED_SQL, current, through_day)
        if blocked is not None:
            return blocked, current
        advanced = await conn.fetchval(_ADVANCE_WATERMARK_SQL, through_day, _WATERMARK)
        if advanced != through_day:
            raise RollupContractError("typed usage rollup watermark advance failed")
        return None, advanced

    async def _advance_watermark_through(
        self, through_day: date
    ) -> tuple[date | None, date | None]:
        assert self._app is not None
        async with self._app.acquire() as conn:
            async with conn.transaction():
                return await self._advance_watermark_in_transaction(conn, through_day)

    async def _seed_bootstrap(self, through_day: date) -> None:
        assert self._audit is not None and self._app is not None
        async with self._app.acquire() as conn:
            started = await conn.fetchval(_BOOTSTRAP_START_SQL)
        if started is None:
            state = await self.bootstrap_state()
            if state.status in {
                BootstrapStatus.RECONCILING,
                BootstrapStatus.COMPLETE,
            }:
                return
            if state.status is not BootstrapStatus.RUNNING:
                raise RollupContractError("bootstrap start compare-and-set failed")

        # Seed audit first. A crash before the following app update is harmless:
        # ON CONFLICT makes the retry idempotent and existing revisions survive.
        async with self._audit.acquire() as conn:
            await conn.fetchrow(
                _SEED_DIRTY_SQL,
                _midnight(through_day + timedelta(days=1)),
            )

        async with self._app.acquire() as conn:
            seeded = await conn.fetchval(_BOOTSTRAP_SEEDED_SQL, through_day)
        if seeded is None:
            state = await self.bootstrap_state()
            if (
                state.status
                not in {
                    BootstrapStatus.RECONCILING,
                    BootstrapStatus.COMPLETE,
                }
                or state.seeded_through_day is None
            ):
                raise RollupContractError("bootstrap seed compare-and-set failed")

    async def _has_pending_revision(self, through_day: date) -> bool:
        assert self._audit is not None
        applied = await self._applied_revisions(through_day)
        items = sorted(applied.items())
        async with self._audit.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                row = await conn.fetchrow(
                    _PENDING_DIRTY_SQL,
                    [item[0] for item in items],
                    [item[1] for item in items],
                    through_day,
                    1,
                )
                return row is not None

    async def _has_pending_coverage_revision(self, through_day: date) -> bool:
        assert self._app is not None
        row = await self._app.fetchrow(
            _STALE_COVERAGE_REVISIONS_SQL,
            through_day,
            1,
        )
        return row is not None

    async def _has_missing_rollup_day(self, through_day: date) -> bool:
        assert self._app is not None
        row = await self._app.fetchrow(
            _MISSING_ROLLUP_DAYS_SQL,
            through_day,
            1,
        )
        return row is not None

    async def _has_pending_work(self, through_day: date) -> bool:
        if await self._has_pending_revision(through_day):
            return True
        if await self._has_pending_coverage_revision(through_day):
            return True
        return await self._has_missing_rollup_day(through_day)

    async def _has_dirty_after(self, through_day: date, cursor: date | None) -> bool:
        assert self._audit is not None
        row = await self._audit.fetchrow(_DIRTY_AFTER_SQL, through_day, cursor, 1)
        return row is not None

    async def _reconcile_batch(
        self,
        through_day: date,
        cursor: date | None,
        limit: int,
    ) -> tuple[int, date | None, bool]:
        assert self._audit is not None and self._app is not None
        dirty_rows = await self._audit.fetch(
            _DIRTY_AFTER_SQL, through_day, cursor, limit
        )
        reconciled = 0
        current_cursor = cursor
        for dirty in dirty_rows:
            day = dirty["day"]
            revision = int(dirty["revision"])
            try:
                snapshot = await self._read_snapshot(day, revision)
            except RollupContractError:
                return reconciled, current_cursor, True
            state = await self._app.fetchrow(_APP_DAY_STATE_SQL, day)
            app_rows = await self._app.fetch(_APP_DAY_ROWS_SQL, day)
            if state is None or int(state["applied_audit_revision"]) != revision:
                return reconciled, current_cursor, True
            typed_app_rows = tuple(DailyUsageRow.from_record(row) for row in app_rows)
            expected = Counter(row.comparison_key() for row in snapshot.rows)
            actual = Counter(row.comparison_key() for row in typed_app_rows)
            if expected != actual:
                return reconciled, current_cursor, True
            advanced = await self._app.fetchval(
                _BOOTSTRAP_RECONCILED_SQL, day, current_cursor
            )
            if advanced is None:
                return reconciled, current_cursor, True
            current_cursor = day
            reconciled += 1
        return reconciled, current_cursor, False

    async def _complete_bootstrap(self, seeded_through: date) -> None:
        assert self._app is not None
        completed = await self._app.fetchval(_BOOTSTRAP_COMPLETE_SQL, seeded_through)
        if completed != BootstrapStatus.COMPLETE.value:
            raise RollupContractError("bootstrap completion compare-and-set failed")

    async def _record_bootstrap_error(self, exc: Exception) -> None:
        if self._app is None:
            return
        # Do not persist exception messages: drivers may include DSNs or SQL
        # parameters. The stable code and exception class are enough to route an
        # operator to logs, where normal secret filtering applies.
        payload = json.dumps(
            {
                "code": "typed-usage-rollup-bootstrap-failed",
                "exception_type": type(exc).__name__,
            },
            separators=(",", ":"),
        )
        try:
            await self._app.execute(_BOOTSTRAP_ERROR_SQL, payload)
        except Exception:
            logger.warning(
                "typed usage bootstrap error state write failed (non-fatal)",
                exc_info=True,
            )


async def typed_usage_rollup_loop(
    shutdown_event: asyncio.Event,
    rollup: TypedUsageDailyRollup | None,
    *,
    interval_s: float = 300.0,
    catchup_delay_s: float = 0.1,
    limit: int = _DEFAULT_LIMIT,
) -> None:
    """Run bootstrap/dirty passes without becoming load-bearing for startup.

    Historical bootstrap drains bounded batches cooperatively instead of paying
    the steady-state five-minute interval per batch. A blocked, mismatched, or
    failed bootstrap falls back to the ordinary interval to avoid a hot loop.
    """

    if rollup is None or not rollup.is_available:
        logger.info("typed usage rollup loop disabled (pools unavailable)")
        return
    while not shutdown_event.is_set():
        result: RollupPassResult | BootstrapStepResult | None = None
        try:
            result = await rollup.run_cycle(limit=limit)
        except Exception:
            logger.exception("typed usage rollup loop iteration failed (non-fatal)")
        catching_up = (
            isinstance(result, BootstrapStepResult)
            and result.status in {BootstrapStatus.RUNNING, BootstrapStatus.RECONCILING}
            and result.blocked_day is None
            and result.error_code is None
            and (
                result.reconciled_days > 0
                or (
                    result.rollup is not None
                    and (
                        result.rollup.selected > 0
                        or result.rollup.applied > 0
                        or result.rollup.stale > 0
                    )
                )
            )
        )
        delay = catchup_delay_s if catching_up else interval_s
        if delay <= 0:
            await asyncio.sleep(0)
            continue
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


__all__ = [
    "ApplyDisposition",
    "AuditDaySnapshot",
    "BootstrapState",
    "BootstrapStatus",
    "BootstrapStepResult",
    "DailyUsageDimensions",
    "DailyUsageRow",
    "DayApplyResult",
    "RollupContractError",
    "RollupPassResult",
    "TypedUsageDailyRollup",
    "legacy_dimensions",
    "typed_usage_rollup_loop",
]
