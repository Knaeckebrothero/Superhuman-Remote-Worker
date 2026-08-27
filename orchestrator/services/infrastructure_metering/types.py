"""Typed, decimal-safe public contracts for usage API v2."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_STORAGE_QUANTUM = Decimal("0.000000000000000001")
_NUMERIC_38_18_INTEGER_DIGITS = 20


def decimal_text(value: Any) -> str:
    """Return one canonical, non-exponent decimal string for the wire/hash edge."""
    if isinstance(value, (bool, float)):
        raise ValueError("decimal value must be Decimal, int, or decimal text")
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, str) and value.strip():
            number = Decimal(value)
        else:
            raise ValueError("decimal value must be Decimal, int, or decimal text")
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid decimal value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError("decimal value must be finite")
    try:
        with localcontext() as context:
            context.prec = 50
            context.rounding = ROUND_HALF_EVEN
            number = number.quantize(_STORAGE_QUANTUM)
    except InvalidOperation as exc:
        raise ValueError("decimal value exceeds NUMERIC(38,18)") from exc
    integer_digits = 1 if number.is_zero() else max(1, number.adjusted() + 1)
    if integer_digits > _NUMERIC_38_18_INTEGER_DIGITS:
        raise ValueError("decimal value exceeds NUMERIC(38,18)")
    if number.is_zero():
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


class MeasurementBasis(StrEnum):
    API_CONSUMED = "api-consumed"
    SCHEDULER_REQUEST = "scheduler-request"
    GUEST_PROVISIONED = "guest-provisioned"
    CLAIM_REQUESTED = "claim-requested"
    VOLUME_PROVISIONED = "volume-provisioned"
    ACTUAL = "actual"
    LEGACY_UNKNOWN = "legacy-unknown"


class AttributionScope(StrEnum):
    CUSTOMER = "customer"
    SHARED_PLATFORM = "shared-platform"
    UNKNOWN = "unknown"


class CostDomain(StrEnum):
    EXTERNAL_SERVICE = "external-service"
    WORKLOAD_ALLOCATION = "workload-allocation"
    PHYSICAL_ASSET = "physical-asset"
    IDLE = "idle"
    OVERHEAD = "overhead"
    UNKNOWN = "unknown"


class ResourceClass(StrEnum):
    LLM_MODEL = "llm-model"
    KUBERNETES_POD = "kubernetes-pod"
    VIRTUAL_MACHINE = "virtual-machine"
    PERSISTENT_VOLUME_CLAIM = "persistent-volume-claim"
    PERSISTENT_VOLUME = "persistent-volume"
    UNKNOWN = "unknown"


class LedgerCostStatus(StrEnum):
    PRICED = "priced"
    PARTIALLY_PRICED = "partially-priced"
    UNPRICED = "unpriced"


class CoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class _ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class UsageWindowV2(_ApiModel):
    start: datetime
    end: datetime
    as_of: datetime
    data_through: datetime | None

    @model_validator(mode="after")
    def _valid_window(self) -> UsageWindowV2:
        values = (self.start, self.end, self.as_of)
        if any(value.tzinfo is None or value.utcoffset() is None for value in values):
            raise ValueError("usage window timestamps must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("usage window end must be after start")
        if self.data_through is not None:
            if (
                self.data_through.tzinfo is None
                or self.data_through.utcoffset() is None
            ):
                raise ValueError("data_through must be timezone-aware")
            if not self.start <= self.data_through <= self.end:
                raise ValueError("data_through must fall within the usage window")
        return self


class UsageLedgerCostV2(_ApiModel):
    status: LedgerCostStatus
    currency: Literal["USD"] = "USD"
    amount: str | None
    priced_quantity: str
    unpriced_quantity: str

    @field_validator("amount", "priced_quantity", "unpriced_quantity")
    @classmethod
    def _canonical_decimals(cls, value: str | None) -> str | None:
        return None if value is None else decimal_text(value)

    @model_validator(mode="after")
    def _valid_cost_coverage(self) -> UsageLedgerCostV2:
        priced = Decimal(self.priced_quantity)
        unpriced = Decimal(self.unpriced_quantity)
        if priced < 0 or unpriced < 0:
            raise ValueError("ledger quantity buckets must be non-negative")
        if self.status == LedgerCostStatus.PRICED.value:
            if self.amount is None or unpriced != 0:
                raise ValueError(
                    "priced cost requires an amount and no unpriced quantity"
                )
        elif self.status == LedgerCostStatus.UNPRICED.value:
            if self.amount is not None or priced != 0:
                raise ValueError(
                    "unpriced cost cannot contain priced quantity or amount"
                )
        elif self.amount is None:
            raise ValueError("partially priced cost requires a priced amount")
        return self


class UsageRowV2(_ApiModel):
    category: str
    measurement_basis: MeasurementBasis
    cost_domain: CostDomain
    resource_class: ResourceClass
    measurement_algorithm: str
    resource: str
    unit: str
    attribution_scope: AttributionScope
    quantity: str
    finalized_quantity: str
    confirmed_provisional_quantity: str
    unverified_projected_quantity: str | None = None
    ledger_cost: UsageLedgerCostV2
    events: int = Field(ge=0)

    @field_validator(
        "quantity",
        "finalized_quantity",
        "confirmed_provisional_quantity",
        "unverified_projected_quantity",
    )
    @classmethod
    def _canonical_quantities(cls, value: str | None) -> str | None:
        return None if value is None else decimal_text(value)

    @model_validator(mode="after")
    def _valid_quantity_partition(self) -> UsageRowV2:
        quantity = Decimal(self.quantity)
        finalized = Decimal(self.finalized_quantity)
        confirmed = Decimal(self.confirmed_provisional_quantity)
        projected = (
            None
            if self.unverified_projected_quantity is None
            else Decimal(self.unverified_projected_quantity)
        )
        if min(quantity, finalized, confirmed) < 0 or (
            projected is not None and projected < 0
        ):
            raise ValueError("usage quantities must be non-negative")
        if quantity != finalized + confirmed:
            raise ValueError("quantity must equal finalized plus confirmed provisional")
        if quantity != (
            Decimal(self.ledger_cost.priced_quantity)
            + Decimal(self.ledger_cost.unpriced_quantity)
        ):
            raise ValueError("ledger quantity buckets must equal quantity")
        return self


class UnknownRangeV2(_ApiModel):
    start: datetime
    end: datetime | None

    @model_validator(mode="after")
    def _valid_range(self) -> UnknownRangeV2:
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("unknown range timestamps must be timezone-aware")
        if self.end is not None:
            if self.end.tzinfo is None or self.end.utcoffset() is None:
                raise ValueError("unknown range timestamps must be timezone-aware")
            if self.end <= self.start:
                raise ValueError("unknown range end must be after start")
        return self


class UsageCoverageV2(_ApiModel):
    status: CoverageStatus
    includes_provisional: bool
    required_sources_ok: int = Field(ge=0)
    required_sources_total: int = Field(ge=0)
    unknown_ranges: list[UnknownRangeV2] = Field(default_factory=list)
    excluded_domains: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _valid_source_counts(self) -> UsageCoverageV2:
        if self.required_sources_ok > self.required_sources_total:
            raise ValueError("required_sources_ok cannot exceed total")
        return self


class UsageSummaryV2(_ApiModel):
    schema_version: Literal[2] = 2
    window: UsageWindowV2
    rows: list[UsageRowV2]
    coverage: UsageCoverageV2

    @model_validator(mode="after")
    def _valid_summary_coverage(self) -> UsageSummaryV2:
        if not self.coverage.includes_provisional and any(
            Decimal(row.confirmed_provisional_quantity) != 0 for row in self.rows
        ):
            raise ValueError("confirmed provisional rows require coverage disclosure")
        for unknown in self.coverage.unknown_ranges:
            if unknown.start < self.window.start:
                raise ValueError("unknown range starts before the usage window")
            if unknown.end is not None and unknown.end > self.window.end:
                raise ValueError("unknown range ends after the usage window")
        return self


def ledger_cost(
    *,
    amount: Any,
    priced_quantity: Any,
    unpriced_quantity: Any,
    priced_events: int | None = None,
    unpriced_events: int | None = None,
) -> UsageLedgerCostV2:
    """Build cost coverage without conflating a free rate with no rate."""
    priced = Decimal(decimal_text(0 if priced_quantity is None else priced_quantity))
    unpriced = Decimal(
        decimal_text(0 if unpriced_quantity is None else unpriced_quantity)
    )
    # Signed correction pairs can move quantity between buckets while the
    # immutable original row remains present. For non-zero net usage, coverage
    # therefore follows the net bucket quantities, not historical row counts.
    # Event counts remain the tie-breaker for genuine zero-quantity events.
    if priced != 0 or unpriced != 0:
        has_priced = priced > 0
        has_unpriced = unpriced > 0
    else:
        has_priced = priced_events is not None and priced_events > 0
        has_unpriced = unpriced_events is not None and unpriced_events > 0
    if has_priced and has_unpriced:
        status = LedgerCostStatus.PARTIALLY_PRICED
    elif has_unpriced:
        status = LedgerCostStatus.UNPRICED
    else:
        # A zero quantity or a non-null zero-cost quantity is fully priced.
        status = LedgerCostStatus.PRICED
        if amount is None and not has_priced:
            amount = 0
    return UsageLedgerCostV2(
        status=status,
        amount=None if amount is None else decimal_text(amount),
        priced_quantity=decimal_text(priced),
        unpriced_quantity=decimal_text(unpriced),
    )
