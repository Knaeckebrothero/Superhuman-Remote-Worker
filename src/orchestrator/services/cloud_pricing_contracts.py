"""Typed, table-agnostic contracts for cloud-equivalent pricing.

The v1 cloud estimator predates lifecycle-aware infrastructure metering and
prices aggregate workspace CPU/RAM rows.  This module is the compatibility
boundary between that estimator and the v2 rate-card/calculator work:

* pricing inputs, matches, exclusions, and calculator results are immutable;
* all quantities and money stay :class:`~decimal.Decimal` until a wire adapter;
* estimate fidelity, input coverage, finality, and rate freshness remain
  independent axes;
* a present zero price is distinguishable from a missing price; and
* the legacy wildcard estimator accepts only its original workspace Pod scope.

Persistence intentionally lives elsewhere.  IDs in these contracts refer to
immutable rate-card/version/component rows without assuming a repository or
database schema is available to the calculator.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Iterable, Mapping, Protocol, runtime_checkable


class PricingContractError(ValueError):
    """Raised when pricing data would violate an exact typed contract."""


class CalculatorKind(StrEnum):
    LINEAR_V1 = "linear_v1"
    EXACT_FLAVOR_V1 = "exact_flavor_v1"
    REFERENCE_DOMINANT_SHARE_V1 = "reference_dominant_share_v1"
    FARGATE_V1 = "fargate_v1"
    ACI_CONTAINER_GROUP_V1 = "aci_container_group_v1"
    BLOCK_VOLUME_V1 = "block_volume_v1"
    AZURE_MANAGED_DISK_V1 = "azure_managed_disk_v1"


class AggregationScope(StrEnum):
    LIFECYCLE = "lifecycle"
    CONCURRENCY_ENVELOPE = "concurrency-envelope"


class ShapeChangePolicy(StrEnum):
    CONTINUE = "continue"
    RESTART = "restart"
    UNSUPPORTED = "unsupported"


class PricingBasis(StrEnum):
    HISTORICAL_PUBLIC_LIST = "historical-public-list"
    CURRENT_PRICE_SCENARIO = "current-price-scenario"


class ModelFidelity(StrEnum):
    EXACT = "exact"
    MODELED = "modeled"
    LOWER_BOUND = "lower_bound"


class InputCoverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class EstimateFinality(StrEnum):
    FINALIZED = "finalized"
    INCLUDES_CONFIRMED_PROVISIONAL = "includes_confirmed_provisional"


class RateStatus(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"


class CostCoverageStatus(StrEnum):
    PRICED = "priced"
    PARTIALLY_PRICED = "partially-priced"
    UNPRICED = "unpriced"


class ExclusionReason(StrEnum):
    """Stable reason codes returned instead of treating an input as free."""

    UNSUPPORTED_CATEGORY = "unsupported-category"
    UNSUPPORTED_UNIT = "unsupported-unit"
    MISSING_REQUIRED_DIMENSION = "missing-required-dimension"
    MEASUREMENT_BASIS_MISMATCH = "measurement-basis-mismatch"
    COST_DOMAIN_MISMATCH = "cost-domain-mismatch"
    RESOURCE_CLASS_MISMATCH = "resource-class-mismatch"
    RESOURCE_MISMATCH = "resource-mismatch"
    ATTRIBUTION_SCOPE_MISMATCH = "attribution-scope-mismatch"
    NO_APPLICABLE_RATE = "no-applicable-rate"
    AMBIGUOUS_RATE = "ambiguous-rate"
    UNSUPPORTED_SHAPE = "unsupported-shape"
    DEFERRED_PRICE_COMPONENT = "deferred-price-component"


def exact_decimal(
    value: Decimal | int | str,
    *,
    field_name: str = "value",
    non_negative: bool = False,
) -> Decimal:
    """Return an exact finite ``Decimal`` without accepting binary floats.

    Database ``NUMERIC`` values arrive as ``Decimal``; API values should arrive
    as decimal strings.  Integers are also exact.  Accepting a ``float`` here
    would make an already-rounded binary value look authoritative, so floats and
    booleans fail loudly at the contract boundary.
    """

    if isinstance(value, (bool, float)):
        raise PricingContractError(
            f"{field_name} must be Decimal, int, or decimal string; floats are forbidden"
        )
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        if not value.strip():
            raise PricingContractError(f"{field_name} must not be empty")
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise PricingContractError(f"{field_name} is not a valid decimal") from exc
    else:
        raise PricingContractError(
            f"{field_name} must be Decimal, int, or decimal string"
        )

    if not parsed.is_finite():
        raise PricingContractError(f"{field_name} must be finite")
    if non_negative and parsed < 0:
        raise PricingContractError(f"{field_name} must be non-negative")
    return Decimal(0) if parsed.is_zero() else parsed


def decimal_to_wire(value: Decimal | int | str) -> str:
    """Serialize a decimal as fixed-point text with no exponent or negative zero."""

    parsed = exact_decimal(value)
    if parsed.is_zero():
        return "0"
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Add finite decimals without depending on the process Decimal context.

    Python's default Decimal precision is 28 significant digits, while the v2
    schema stores ``NUMERIC(38,18)``. Normal ``sum`` can therefore round valid
    ledger values. Aligning integer coefficients at a shared exponent keeps the
    operation exact regardless of ambient context.
    """

    numbers = tuple(values)
    if not numbers:
        return Decimal(0)
    if any(not number.is_finite() for number in numbers):
        raise PricingContractError("cannot sum non-finite decimals")

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
    digits = tuple(int(digit) for digit in str(abs(total)))
    return Decimal((int(total < 0), digits, exponent))


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PricingContractError(f"{field_name} must be a non-empty string")
    return value


def _currency(value: str) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha():
        raise PricingContractError("currency must be a three-letter ISO-style code")
    normalized = value.upper()
    if value != normalized:
        raise PricingContractError("currency must be uppercase")
    return normalized


def _aware_timestamp(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PricingContractError(f"{field_name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class PricingQuality:
    """Independent truth axes for one component or aggregate estimate."""

    model_fidelity: ModelFidelity
    input_coverage: InputCoverage
    finality: EstimateFinality
    rate_status: RateStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "model_fidelity", ModelFidelity(self.model_fidelity))
        object.__setattr__(self, "input_coverage", InputCoverage(self.input_coverage))
        object.__setattr__(self, "finality", EstimateFinality(self.finality))
        object.__setattr__(self, "rate_status", RateStatus(self.rate_status))

    def to_wire(self) -> dict[str, str]:
        return {
            "model_fidelity": self.model_fidelity.value,
            "input_coverage": self.input_coverage.value,
            "finality": self.finality.value,
            "rate_status": self.rate_status.value,
        }


def combine_pricing_quality(qualities: Iterable[PricingQuality]) -> PricingQuality:
    """Conservatively combine component qualities without merging their axes."""

    values = tuple(qualities)
    if not values:
        return PricingQuality(
            ModelFidelity.EXACT,
            InputCoverage.COMPLETE,
            EstimateFinality.FINALIZED,
            RateStatus.FRESH,
        )

    fidelities = {quality.model_fidelity for quality in values}
    if ModelFidelity.LOWER_BOUND in fidelities:
        fidelity = ModelFidelity.LOWER_BOUND
    elif ModelFidelity.MODELED in fidelities:
        fidelity = ModelFidelity.MODELED
    else:
        fidelity = ModelFidelity.EXACT

    coverages = {quality.input_coverage for quality in values}
    if coverages == {InputCoverage.COMPLETE}:
        coverage = InputCoverage.COMPLETE
    elif coverages == {InputCoverage.UNAVAILABLE}:
        coverage = InputCoverage.UNAVAILABLE
    else:
        coverage = InputCoverage.PARTIAL

    finality = (
        EstimateFinality.INCLUDES_CONFIRMED_PROVISIONAL
        if any(
            quality.finality == EstimateFinality.INCLUDES_CONFIRMED_PROVISIONAL
            for quality in values
        )
        else EstimateFinality.FINALIZED
    )

    statuses = {quality.rate_status for quality in values}
    if RateStatus.MISSING in statuses:
        rate_status = RateStatus.MISSING
    elif RateStatus.STALE in statuses:
        rate_status = RateStatus.STALE
    else:
        rate_status = RateStatus.FRESH

    return PricingQuality(fidelity, coverage, finality, rate_status)


@dataclass(frozen=True, slots=True)
class PricingDimensions:
    category: str
    measurement_basis: str
    cost_domain: str
    resource_class: str
    resource: str
    unit: str
    attribution_scope: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "category",
            "measurement_basis",
            "cost_domain",
            "resource_class",
            "resource",
            "unit",
        ):
            _required_text(getattr(self, field_name), field_name)
        if self.attribution_scope is not None:
            _required_text(self.attribution_scope, "attribution_scope")

    def key(self) -> tuple[str, str, str, str, str, str, str | None]:
        """Return the complete matching key; callers must not drop dimensions."""

        return (
            self.category,
            self.measurement_basis,
            self.cost_domain,
            self.resource_class,
            self.resource,
            self.unit,
            self.attribution_scope,
        )


@dataclass(frozen=True, slots=True)
class PricingInput:
    input_id: str
    dimensions: PricingDimensions
    quantity: Decimal
    source_lifecycle_id: str | None = None
    billing_occurrence_id: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    finality: EstimateFinality = EstimateFinality.FINALIZED

    def __post_init__(self) -> None:
        _required_text(self.input_id, "input_id")
        object.__setattr__(
            self,
            "quantity",
            exact_decimal(self.quantity, field_name="quantity", non_negative=True),
        )
        object.__setattr__(self, "finality", EstimateFinality(self.finality))
        if (self.period_start is None) != (self.period_end is None):
            raise PricingContractError(
                "period_start and period_end must be provided together"
            )
        if self.period_start is not None and self.period_end is not None:
            _aware_timestamp(self.period_start, "period_start")
            _aware_timestamp(self.period_end, "period_end")
            if self.period_end <= self.period_start:
                raise PricingContractError("period_end must be after period_start")
        if self.source_lifecycle_id is not None:
            _required_text(self.source_lifecycle_id, "source_lifecycle_id")
        if self.billing_occurrence_id is not None:
            _required_text(self.billing_occurrence_id, "billing_occurrence_id")


@dataclass(frozen=True, slots=True)
class MatchedRateComponent:
    """Immutable in-memory view of one selected provider price component."""

    component_id: str
    component: str
    billing_unit: str
    unit_size: Decimal
    unit_price: Decimal
    source_sku: str | None = None
    source_meter: str | None = None
    tier_min: Decimal | None = None
    tier_max: Decimal | None = None
    included_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        _required_text(self.component_id, "component_id")
        _required_text(self.component, "component")
        _required_text(self.billing_unit, "billing_unit")
        object.__setattr__(
            self,
            "unit_size",
            exact_decimal(self.unit_size, field_name="unit_size", non_negative=True),
        )
        if self.unit_size == 0:
            raise PricingContractError("unit_size must be greater than zero")
        object.__setattr__(
            self,
            "unit_price",
            exact_decimal(self.unit_price, field_name="unit_price", non_negative=True),
        )
        for field_name in ("tier_min", "tier_max", "included_quantity"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    exact_decimal(value, field_name=field_name, non_negative=True),
                )
        if (
            self.tier_min is not None
            and self.tier_max is not None
            and self.tier_max <= self.tier_min
        ):
            raise PricingContractError("tier_max must be greater than tier_min")


@dataclass(frozen=True, slots=True)
class PricingMatch:
    """A deterministic card/version match ready for a typed calculator."""

    card_id: str
    version_id: str
    provider: str
    target_service: str
    target_region: str
    currency: str
    pricing_basis: PricingBasis
    calculator: CalculatorKind
    aggregation_scope: AggregationScope
    shape_change_policy: ShapeChangePolicy
    inputs: tuple[PricingInput, ...]
    rate_components: tuple[MatchedRateComponent, ...]

    def __post_init__(self) -> None:
        for field_name in (
            "card_id",
            "version_id",
            "provider",
            "target_service",
            "target_region",
        ):
            _required_text(getattr(self, field_name), field_name)
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "pricing_basis", PricingBasis(self.pricing_basis))
        object.__setattr__(self, "calculator", CalculatorKind(self.calculator))
        object.__setattr__(
            self, "aggregation_scope", AggregationScope(self.aggregation_scope)
        )
        object.__setattr__(
            self, "shape_change_policy", ShapeChangePolicy(self.shape_change_policy)
        )
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "rate_components", tuple(self.rate_components))
        if not self.inputs:
            raise PricingContractError("a pricing match requires at least one input")
        if not self.rate_components:
            raise PricingContractError(
                "a pricing match requires at least one rate component"
            )
        expected_scope = (
            AggregationScope.CONCURRENCY_ENVELOPE
            if self.calculator == CalculatorKind.REFERENCE_DOMINANT_SHARE_V1
            else AggregationScope.LIFECYCLE
        )
        if self.aggregation_scope != expected_scope:
            raise PricingContractError(
                f"{self.calculator.value} requires {expected_scope.value} aggregation"
            )


@dataclass(frozen=True, slots=True)
class PricingExclusion:
    input_id: str | None
    reason: ExclusionReason
    detail: str = ""
    dimensions: PricingDimensions | None = None
    quantity: Decimal | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason", ExclusionReason(self.reason))
        if self.input_id is not None:
            _required_text(self.input_id, "input_id")
        if self.quantity is not None:
            object.__setattr__(
                self,
                "quantity",
                exact_decimal(self.quantity, field_name="quantity", non_negative=True),
            )

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "input_id": self.input_id,
            "reason": self.reason.value,
            "detail": self.detail,
            "quantity": (
                decimal_to_wire(self.quantity) if self.quantity is not None else None
            ),
        }
        if self.dimensions is not None:
            payload["dimensions"] = {
                "category": self.dimensions.category,
                "measurement_basis": self.dimensions.measurement_basis,
                "cost_domain": self.dimensions.cost_domain,
                "resource_class": self.dimensions.resource_class,
                "resource": self.dimensions.resource,
                "unit": self.dimensions.unit,
                "attribution_scope": self.dimensions.attribution_scope,
            }
        return payload


@dataclass(frozen=True, slots=True)
class MatchResult:
    matches: tuple[PricingMatch, ...] = ()
    exclusions: tuple[PricingExclusion, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "matches", tuple(self.matches))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))


@dataclass(frozen=True, slots=True)
class PricedComponent:
    """One provider charge line; measured and billed quantities stay distinct."""

    rate_component_id: str
    currency: str
    measured_quantity: Decimal
    billed_quantity: Decimal
    billing_unit: str
    unit_price: Decimal
    amount: Decimal
    quality: PricingQuality
    input_ids: tuple[str, ...] = ()
    charge_period_start: datetime | None = None
    charge_period_end: datetime | None = None
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.rate_component_id, "rate_component_id")
        object.__setattr__(self, "currency", _currency(self.currency))
        _required_text(self.billing_unit, "billing_unit")
        for field_name in (
            "measured_quantity",
            "billed_quantity",
            "unit_price",
            "amount",
        ):
            object.__setattr__(
                self,
                field_name,
                exact_decimal(
                    getattr(self, field_name),
                    field_name=field_name,
                    non_negative=True,
                ),
            )
        object.__setattr__(self, "input_ids", tuple(self.input_ids))
        object.__setattr__(self, "assumptions", tuple(self.assumptions))
        if (self.charge_period_start is None) != (self.charge_period_end is None):
            raise PricingContractError(
                "charge period start and end must be provided together"
            )
        if self.charge_period_start is not None and self.charge_period_end is not None:
            _aware_timestamp(self.charge_period_start, "charge_period_start")
            _aware_timestamp(self.charge_period_end, "charge_period_end")
            if self.charge_period_end <= self.charge_period_start:
                raise PricingContractError(
                    "charge_period_end must be after charge_period_start"
                )

    def to_wire(self) -> dict[str, Any]:
        return {
            "rate_component_id": self.rate_component_id,
            "currency": self.currency,
            "measured_quantity": decimal_to_wire(self.measured_quantity),
            "billed_quantity": decimal_to_wire(self.billed_quantity),
            "billing_unit": self.billing_unit,
            "unit_price": decimal_to_wire(self.unit_price),
            "amount": decimal_to_wire(self.amount),
            "quality": self.quality.to_wire(),
            "input_ids": list(self.input_ids),
            "charge_period_start": (
                self.charge_period_start.isoformat()
                if self.charge_period_start is not None
                else None
            ),
            "charge_period_end": (
                self.charge_period_end.isoformat()
                if self.charge_period_end is not None
                else None
            ),
            "assumptions": list(self.assumptions),
        }


@dataclass(frozen=True, slots=True)
class CalculatorResult:
    card_id: str
    version_id: str
    calculator: CalculatorKind
    currency: str
    quality: PricingQuality
    components: tuple[PricedComponent, ...] = ()
    exclusions: tuple[PricingExclusion, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.card_id, "card_id")
        _required_text(self.version_id, "version_id")
        object.__setattr__(self, "calculator", CalculatorKind(self.calculator))
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "components", tuple(self.components))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        if any(component.currency != self.currency for component in self.components):
            raise PricingContractError(
                "calculator result cannot contain mixed component currencies"
            )

    @property
    def amount(self) -> Decimal | None:
        if not self.components:
            return None
        return _exact_sum(component.amount for component in self.components)

    @property
    def cost_coverage_status(self) -> CostCoverageStatus:
        return derive_cost_coverage_status(
            priced_events=len(self.components),
            unpriced_events=len(self.exclusions),
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "version_id": self.version_id,
            "calculator": self.calculator.value,
            "currency": self.currency,
            "amount": decimal_to_wire(self.amount) if self.amount is not None else None,
            "cost_status": self.cost_coverage_status.value,
            "quality": self.quality.to_wire(),
            "components": [component.to_wire() for component in self.components],
            "exclusions": [exclusion.to_wire() for exclusion in self.exclusions],
        }


@runtime_checkable
class PricingCalculator(Protocol):
    """Contract implemented by each code-versioned provider calculator."""

    calculator: CalculatorKind
    aggregation_scope: AggregationScope

    def calculate(self, match: PricingMatch) -> CalculatorResult: ...


@dataclass(frozen=True, slots=True)
class CostContribution:
    """One same-dimension canonical cost contribution.

    ``amount=None`` means no rate was selected.  ``amount=Decimal(0)`` means a
    real zero rate and therefore counts as priced.  Signed values are allowed so
    append-only correction rows aggregate faithfully.
    """

    currency: str
    quantity: Decimal
    amount: Decimal | None
    events: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(
            self, "quantity", exact_decimal(self.quantity, field_name="quantity")
        )
        if self.amount is not None:
            object.__setattr__(
                self, "amount", exact_decimal(self.amount, field_name="amount")
            )
        if isinstance(self.events, bool) or not isinstance(self.events, int):
            raise PricingContractError("events must be an integer")
        if self.events <= 0:
            raise PricingContractError("events must be greater than zero")

    @property
    def is_priced(self) -> bool:
        return self.amount is not None


@dataclass(frozen=True, slots=True)
class CostAggregate:
    status: CostCoverageStatus
    currency: str
    amount: Decimal | None
    quantity: Decimal
    priced_quantity: Decimal
    unpriced_quantity: Decimal
    priced_events: int
    unpriced_events: int
    events: int

    def to_wire(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "currency": self.currency,
            "amount": decimal_to_wire(self.amount) if self.amount is not None else None,
            "quantity": decimal_to_wire(self.quantity),
            "priced_quantity": decimal_to_wire(self.priced_quantity),
            "unpriced_quantity": decimal_to_wire(self.unpriced_quantity),
            "priced_events": self.priced_events,
            "unpriced_events": self.unpriced_events,
            "events": self.events,
        }


def derive_cost_coverage_status(
    *,
    priced_events: int,
    unpriced_events: int,
    priced_quantity: Decimal | int | str | None = None,
    unpriced_quantity: Decimal | int | str | None = None,
) -> CostCoverageStatus:
    """Derive coverage from net buckets, with counts for an all-zero tie."""

    if priced_events < 0 or unpriced_events < 0:
        raise PricingContractError("priced/unpriced event counts must be non-negative")
    if (priced_quantity is None) != (unpriced_quantity is None):
        raise PricingContractError("both priced/unpriced quantities are required")
    if priced_quantity is not None and unpriced_quantity is not None:
        priced = exact_decimal(priced_quantity, field_name="priced_quantity")
        unpriced = exact_decimal(unpriced_quantity, field_name="unpriced_quantity")
        if priced < 0 or unpriced < 0:
            raise PricingContractError(
                "net price-coverage buckets must be non-negative"
            )
        if priced != 0 or unpriced != 0:
            if priced and unpriced:
                return CostCoverageStatus.PARTIALLY_PRICED
            if priced:
                return CostCoverageStatus.PRICED
            return CostCoverageStatus.UNPRICED
    if priced_events and unpriced_events:
        return CostCoverageStatus.PARTIALLY_PRICED
    if priced_events:
        return CostCoverageStatus.PRICED
    return CostCoverageStatus.UNPRICED


def aggregate_cost_contributions(
    contributions: Iterable[CostContribution],
    *,
    currency: str | None = None,
) -> CostAggregate:
    """Aggregate already-grouped, same-unit costs with exact Decimal arithmetic.

    The caller must group by the complete quantity dimension tuple before using
    this helper; adding CPU-hours to GiB-hours remains invalid.  Mixed currencies
    fail rather than acquiring an implicit exchange rate.
    """

    rows = tuple(contributions)
    expected_currency = _currency(currency) if currency is not None else None
    currencies = {row.currency for row in rows}
    if expected_currency is not None:
        if any(row_currency != expected_currency for row_currency in currencies):
            raise PricingContractError("cost contribution currency mismatch")
    elif len(currencies) > 1:
        raise PricingContractError("cannot aggregate mixed currencies")
    elif currencies:
        expected_currency = next(iter(currencies))
    else:
        raise PricingContractError(
            "currency is required when aggregating an empty contribution set"
        )

    priced = tuple(row for row in rows if row.is_priced)
    unpriced = tuple(row for row in rows if not row.is_priced)
    priced_events = sum(row.events for row in priced)
    unpriced_events = sum(row.events for row in unpriced)
    amount = (
        _exact_sum(row.amount for row in priced if row.amount is not None)
        if priced_events
        else None
    )
    priced_quantity = _exact_sum(row.quantity for row in priced)
    unpriced_quantity = _exact_sum(row.quantity for row in unpriced)
    return CostAggregate(
        status=derive_cost_coverage_status(
            priced_events=priced_events,
            unpriced_events=unpriced_events,
            priced_quantity=priced_quantity,
            unpriced_quantity=unpriced_quantity,
        ),
        currency=expected_currency,
        amount=amount,
        quantity=_exact_sum((priced_quantity, unpriced_quantity)),
        priced_quantity=priced_quantity,
        unpriced_quantity=unpriced_quantity,
        priced_events=priced_events,
        unpriced_events=unpriced_events,
        events=priced_events + unpriced_events,
    )


_V1_WORKSPACE_UNITS = frozenset({"vcpu-hour", "gib-hour"})
_V1_TYPED_FIELDS = (
    "measurement_basis",
    "cost_domain",
    "resource_class",
    "resource",
    "attribution_scope",
)
_V1_EXPECTED_DIMENSIONS = {
    "measurement_basis": "scheduler-request",
    "cost_domain": "workload-allocation",
    "resource_class": "kubernetes-pod",
    "resource": "workspace_pod",
    "attribution_scope": "customer",
}
_V1_MISMATCH_REASONS = {
    "measurement_basis": ExclusionReason.MEASUREMENT_BASIS_MISMATCH,
    "cost_domain": ExclusionReason.COST_DOMAIN_MISMATCH,
    "resource_class": ExclusionReason.RESOURCE_CLASS_MISMATCH,
    "resource": ExclusionReason.RESOURCE_MISMATCH,
    "attribution_scope": ExclusionReason.ATTRIBUTION_SCOPE_MISMATCH,
}


@dataclass(frozen=True, slots=True)
class V1WorkspaceEligibility:
    eligible: bool
    reason: ExclusionReason | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.reason is not None:
            object.__setattr__(self, "reason", ExclusionReason(self.reason))
        if self.eligible and self.reason is not None:
            raise PricingContractError(
                "an eligible row cannot have an exclusion reason"
            )
        if not self.eligible and self.reason is None:
            raise PricingContractError("an excluded row requires an exclusion reason")


@dataclass(frozen=True, slots=True)
class V1WorkspaceRowExclusion:
    row_index: int
    reason: ExclusionReason
    detail: str
    category: str | None
    resource: str | None
    unit: str | None


@dataclass(frozen=True, slots=True)
class V1WorkspaceSelection:
    eligible_rows: tuple[Mapping[str, Any], ...]
    exclusions: tuple[V1WorkspaceRowExclusion, ...]


def classify_v1_workspace_usage_row(
    row: Mapping[str, Any],
) -> V1WorkspaceEligibility:
    """Classify one aggregate row for the legacy wildcard cloud cards.

    Rows with none of the v2 discriminator fields are the original v1 aggregate
    shape.  Before v2, all compute CPU/RAM quantities came from workspace Pods,
    so that exact legacy shape remains eligible.  Once any typed discriminator
    appears, every discriminator must be present and match the workspace-only
    tuple; partial typed rows fail closed.
    """

    category = row.get("category")
    unit = row.get("unit")
    if category != "compute":
        return V1WorkspaceEligibility(
            False,
            ExclusionReason.UNSUPPORTED_CATEGORY,
            "legacy cloud cards price compute rows only",
        )
    if unit not in _V1_WORKSPACE_UNITS:
        return V1WorkspaceEligibility(
            False,
            ExclusionReason.UNSUPPORTED_UNIT,
            "legacy cloud cards price workspace vCPU-hour and GiB-hour only",
        )

    has_typed_dimension = any(field_name in row for field_name in _V1_TYPED_FIELDS)
    if not has_typed_dimension:
        return V1WorkspaceEligibility(True)

    missing = [field_name for field_name in _V1_TYPED_FIELDS if field_name not in row]
    if missing:
        return V1WorkspaceEligibility(
            False,
            ExclusionReason.MISSING_REQUIRED_DIMENSION,
            f"typed row is missing: {', '.join(missing)}",
        )

    for field_name, expected in _V1_EXPECTED_DIMENSIONS.items():
        actual = row.get(field_name)
        if actual != expected:
            return V1WorkspaceEligibility(
                False,
                _V1_MISMATCH_REASONS[field_name],
                f"{field_name}={actual!r}; expected {expected!r}",
            )
    return V1WorkspaceEligibility(True)


def is_v1_workspace_usage_row(row: Mapping[str, Any]) -> bool:
    """Return whether a row may enter the legacy wildcard estimator."""

    return classify_v1_workspace_usage_row(row).eligible


def partition_v1_workspace_usage(
    rows: Iterable[Mapping[str, Any]],
) -> V1WorkspaceSelection:
    """Return eligible v1 rows plus stable exclusions for coverage reporting."""

    eligible: list[Mapping[str, Any]] = []
    exclusions: list[V1WorkspaceRowExclusion] = []
    for index, row in enumerate(rows):
        decision = classify_v1_workspace_usage_row(row)
        if decision.eligible:
            eligible.append(row)
            continue
        assert decision.reason is not None
        exclusions.append(
            V1WorkspaceRowExclusion(
                row_index=index,
                reason=decision.reason,
                detail=decision.detail,
                category=(str(row["category"]) if row.get("category") else None),
                resource=(str(row["resource"]) if row.get("resource") else None),
                unit=(str(row["unit"]) if row.get("unit") else None),
            )
        )
    return V1WorkspaceSelection(tuple(eligible), tuple(exclusions))


__all__ = [
    "AggregationScope",
    "CalculatorKind",
    "CalculatorResult",
    "CostAggregate",
    "CostContribution",
    "CostCoverageStatus",
    "EstimateFinality",
    "ExclusionReason",
    "InputCoverage",
    "MatchResult",
    "MatchedRateComponent",
    "ModelFidelity",
    "PricedComponent",
    "PricingBasis",
    "PricingCalculator",
    "PricingContractError",
    "PricingDimensions",
    "PricingExclusion",
    "PricingInput",
    "PricingMatch",
    "PricingQuality",
    "RateStatus",
    "ShapeChangePolicy",
    "V1WorkspaceEligibility",
    "V1WorkspaceRowExclusion",
    "V1WorkspaceSelection",
    "aggregate_cost_contributions",
    "classify_v1_workspace_usage_row",
    "combine_pricing_quality",
    "decimal_to_wire",
    "derive_cost_coverage_status",
    "exact_decimal",
    "is_v1_workspace_usage_row",
    "partition_v1_workspace_usage",
]
