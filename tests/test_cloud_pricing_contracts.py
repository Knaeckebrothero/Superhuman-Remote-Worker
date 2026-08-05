"""Typed cloud-pricing contract and compatibility-gate tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from orchestrator.services.cloud_pricing_contracts import (
    AggregationScope,
    CalculatorKind,
    CalculatorResult,
    CostContribution,
    CostCoverageStatus,
    EstimateFinality,
    ExclusionReason,
    InputCoverage,
    MatchedRateComponent,
    ModelFidelity,
    PricedComponent,
    PricingBasis,
    PricingContractError,
    PricingDimensions,
    PricingExclusion,
    PricingInput,
    PricingMatch,
    PricingQuality,
    RateStatus,
    ShapeChangePolicy,
    aggregate_cost_contributions,
    classify_v1_workspace_usage_row,
    combine_pricing_quality,
    decimal_to_wire,
    exact_decimal,
    partition_v1_workspace_usage,
)


def _quality(
    *,
    fidelity: ModelFidelity = ModelFidelity.EXACT,
    coverage: InputCoverage = InputCoverage.COMPLETE,
    finality: EstimateFinality = EstimateFinality.FINALIZED,
    rate_status: RateStatus = RateStatus.FRESH,
) -> PricingQuality:
    return PricingQuality(fidelity, coverage, finality, rate_status)


def _workspace_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "category": "compute",
        "measurement_basis": "scheduler-request",
        "cost_domain": "workload-allocation",
        "resource_class": "kubernetes-pod",
        "resource": "workspace_pod",
        "unit": "vcpu-hour",
        "attribution_scope": "customer",
        "quantity": "1",
    }
    row.update(changes)
    return row


@pytest.mark.parametrize("value", [0.1, True, "NaN", "Infinity", ""])
def test_exact_decimal_rejects_inexact_or_non_finite_values(value: object):
    with pytest.raises(PricingContractError):
        exact_decimal(value)  # type: ignore[arg-type]


def test_decimal_wire_format_is_exact_and_non_exponent():
    assert decimal_to_wire(Decimal("1.2300")) == "1.23"
    assert decimal_to_wire(Decimal("1E+3")) == "1000"
    assert decimal_to_wire(Decimal("1E-18")) == "0.000000000000000001"
    assert decimal_to_wire(Decimal("-0.000")) == "0"


def test_free_zero_is_priced_instead_of_unpriced():
    aggregate = aggregate_cost_contributions(
        [CostContribution("USD", quantity="4", amount="0", events=2)]
    )

    assert aggregate.status is CostCoverageStatus.PRICED
    assert aggregate.amount == Decimal(0)
    assert aggregate.priced_quantity == Decimal(4)
    assert aggregate.unpriced_quantity == Decimal(0)
    assert aggregate.to_wire()["amount"] == "0"


def test_missing_rate_keeps_cost_null_and_quantity_unpriced():
    aggregate = aggregate_cost_contributions(
        [CostContribution("USD", quantity="4", amount=None, events=2)]
    )

    assert aggregate.status is CostCoverageStatus.UNPRICED
    assert aggregate.amount is None
    assert aggregate.priced_events == 0
    assert aggregate.unpriced_events == 2
    assert aggregate.unpriced_quantity == Decimal(4)


def test_mixed_rate_coverage_sums_only_known_cost():
    aggregate = aggregate_cost_contributions(
        [
            CostContribution("USD", quantity="2.5", amount="1.125", events=3),
            CostContribution("USD", quantity="1.5", amount=None, events=2),
        ]
    )

    assert aggregate.status is CostCoverageStatus.PARTIALLY_PRICED
    assert aggregate.amount == Decimal("1.125")
    assert aggregate.quantity == Decimal("4.0")
    assert aggregate.priced_quantity == Decimal("2.5")
    assert aggregate.unpriced_quantity == Decimal("1.5")
    assert aggregate.events == 5


def test_signed_corrections_remain_exact():
    aggregate = aggregate_cost_contributions(
        [
            CostContribution("USD", quantity="3", amount="0.3"),
            CostContribution("USD", quantity="-1", amount="-0.1"),
        ]
    )

    assert aggregate.status is CostCoverageStatus.PRICED
    assert aggregate.quantity == Decimal(2)
    assert aggregate.amount == Decimal("0.2")


def test_signed_pricing_correction_uses_net_coverage_buckets():
    aggregate = aggregate_cost_contributions(
        [
            CostContribution("USD", quantity="4", amount=None),
            CostContribution("USD", quantity="-4", amount=None),
            CostContribution("USD", quantity="4", amount="1"),
        ]
    )

    assert aggregate.status is CostCoverageStatus.PRICED
    assert aggregate.quantity == Decimal(4)
    assert aggregate.priced_quantity == Decimal(4)
    assert aggregate.unpriced_quantity == Decimal(0)
    assert aggregate.unpriced_events == 2


def test_aggregation_does_not_round_at_the_default_decimal_precision():
    aggregate = aggregate_cost_contributions(
        [
            CostContribution(
                "USD",
                quantity="99999999999999999999.999999999999999999",
                amount="99999999999999999999.999999999999999999",
            ),
            CostContribution(
                "USD",
                quantity="0.000000000000000001",
                amount="0.000000000000000001",
            ),
        ]
    )

    expected = Decimal("100000000000000000000.000000000000000000")
    assert aggregate.quantity == expected
    assert aggregate.amount == expected


def test_aggregate_refuses_an_implicit_currency_conversion():
    with pytest.raises(PricingContractError, match="mixed currencies"):
        aggregate_cost_contributions(
            [
                CostContribution("USD", quantity="1", amount="1"),
                CostContribution("EUR", quantity="1", amount="1"),
            ]
        )


def test_quality_axes_are_combined_independently():
    combined = combine_pricing_quality(
        [
            _quality(fidelity=ModelFidelity.EXACT),
            _quality(
                fidelity=ModelFidelity.LOWER_BOUND,
                coverage=InputCoverage.PARTIAL,
                finality=EstimateFinality.INCLUDES_CONFIRMED_PROVISIONAL,
                rate_status=RateStatus.STALE,
            ),
        ]
    )

    assert combined == PricingQuality(
        ModelFidelity.LOWER_BOUND,
        InputCoverage.PARTIAL,
        EstimateFinality.INCLUDES_CONFIRMED_PROVISIONAL,
        RateStatus.STALE,
    )

    assert (
        combine_pricing_quality(
            [
                _quality(fidelity=ModelFidelity.MODELED),
                _quality(fidelity=ModelFidelity.LOWER_BOUND),
            ]
        ).model_fidelity
        is ModelFidelity.LOWER_BOUND
    )


def test_match_and_result_are_immutable_decimal_safe_contracts():
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    dimensions = PricingDimensions(
        category="compute",
        measurement_basis="scheduler-request",
        cost_domain="workload-allocation",
        resource_class="kubernetes-pod",
        resource="workspace_pod",
        unit="vcpu-hour",
        attribution_scope="customer",
    )
    pricing_input = PricingInput(
        input_id="input-1",
        dimensions=dimensions,
        quantity="1.25",
        source_lifecycle_id="pod-1",
        billing_occurrence_id="task-1",
        period_start=start,
        period_end=start + timedelta(hours=1),
    )
    rate = MatchedRateComponent(
        component_id="component-1",
        component="cpu",
        billing_unit="vcpu-hour",
        unit_size="1",
        unit_price="0.04656",
        source_sku="sku-1",
    )
    match = PricingMatch(
        card_id="card-1",
        version_id="version-1",
        provider="aws",
        target_service="fargate",
        target_region="eu-central-1",
        currency="USD",
        pricing_basis=PricingBasis.HISTORICAL_PUBLIC_LIST,
        calculator=CalculatorKind.FARGATE_V1,
        aggregation_scope=AggregationScope.LIFECYCLE,
        shape_change_policy=ShapeChangePolicy.RESTART,
        inputs=[pricing_input],  # type: ignore[arg-type]
        rate_components=[rate],  # type: ignore[arg-type]
    )
    component = PricedComponent(
        rate_component_id=rate.component_id,
        currency="USD",
        measured_quantity="1.25",
        billed_quantity="1.25",
        billing_unit="vcpu-hour",
        unit_price="0.04656",
        amount="0.0582",
        quality=_quality(),
        input_ids=[pricing_input.input_id],  # type: ignore[arg-type]
        charge_period_start=start,
        charge_period_end=start + timedelta(hours=1),
    )
    result = CalculatorResult(
        card_id=match.card_id,
        version_id=match.version_id,
        calculator=match.calculator,
        currency=match.currency,
        quality=_quality(),
        components=[component],  # type: ignore[arg-type]
    )

    assert isinstance(match.inputs, tuple)
    assert result.amount == Decimal("0.0582")
    assert result.to_wire()["amount"] == "0.0582"
    with pytest.raises(FrozenInstanceError):
        pricing_input.quantity = Decimal(2)  # type: ignore[misc]


def test_match_rejects_a_calculator_at_the_wrong_aggregation_scope():
    start = datetime(2026, 8, 5, tzinfo=timezone.utc)
    pricing_input = PricingInput(
        input_id="input-1",
        dimensions=PricingDimensions(
            category="compute",
            measurement_basis="scheduler-request",
            cost_domain="workload-allocation",
            resource_class="kubernetes-pod",
            resource="workspace_pod",
            unit="vcpu-hour",
        ),
        quantity="1",
        period_start=start,
        period_end=start + timedelta(hours=1),
    )
    rate = MatchedRateComponent(
        component_id="cpu",
        component="cpu",
        billing_unit="vcpu-hour",
        unit_size="1",
        unit_price="1",
    )
    with pytest.raises(PricingContractError, match="concurrency-envelope"):
        PricingMatch(
            card_id="card",
            version_id="version",
            provider="stackit",
            target_service="ske",
            target_region="eu01",
            currency="EUR",
            pricing_basis=PricingBasis.CURRENT_PRICE_SCENARIO,
            calculator=CalculatorKind.REFERENCE_DOMINANT_SHARE_V1,
            aggregation_scope=AggregationScope.LIFECYCLE,
            shape_change_policy=ShapeChangePolicy.CONTINUE,
            inputs=(pricing_input,),
            rate_components=(rate,),
        )


def test_result_reports_partial_when_a_component_is_excluded():
    result = CalculatorResult(
        card_id="card-1",
        version_id="version-1",
        calculator=CalculatorKind.LINEAR_V1,
        currency="USD",
        quality=_quality(coverage=InputCoverage.PARTIAL),
        components=[
            PricedComponent(
                rate_component_id="free-cpu",
                currency="USD",
                measured_quantity="1",
                billed_quantity="1",
                billing_unit="vcpu-hour",
                unit_price="0",
                amount="0",
                quality=_quality(),
            )
        ],  # type: ignore[arg-type]
        exclusions=[
            PricingExclusion(
                input_id="ram-1",
                reason=ExclusionReason.NO_APPLICABLE_RATE,
                quantity="4",
            )
        ],  # type: ignore[arg-type]
    )

    assert result.amount == Decimal(0)
    assert result.cost_coverage_status is CostCoverageStatus.PARTIALLY_PRICED
    assert result.to_wire()["amount"] == "0"


def test_v1_gate_retains_the_exact_legacy_aggregate_shape():
    decision = classify_v1_workspace_usage_row(
        {"category": "compute", "unit": "gib-hour", "quantity": 4}
    )
    assert decision.eligible
    assert decision.reason is None


def test_v1_gate_accepts_only_the_complete_typed_workspace_tuple():
    assert classify_v1_workspace_usage_row(_workspace_row()).eligible

    partial = _workspace_row()
    del partial["measurement_basis"]
    decision = classify_v1_workspace_usage_row(partial)
    assert not decision.eligible
    assert decision.reason is ExclusionReason.MISSING_REQUIRED_DIMENSION


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"resource": "agent_pod"}, ExclusionReason.RESOURCE_MISMATCH),
        (
            {
                "measurement_basis": "guest-provisioned",
                "resource_class": "virtual-machine",
                "resource": "job_vm",
            },
            ExclusionReason.MEASUREMENT_BASIS_MISMATCH,
        ),
        (
            {"attribution_scope": "shared-platform"},
            ExclusionReason.ATTRIBUTION_SCOPE_MISMATCH,
        ),
        (
            {
                "category": "storage",
                "measurement_basis": "claim-requested",
                "resource_class": "persistent-volume-claim",
                "resource": "workspace_pvc",
                "unit": "gib-hour",
            },
            ExclusionReason.UNSUPPORTED_CATEGORY,
        ),
    ],
)
def test_v1_gate_excludes_heterogeneous_infrastructure_classes(
    changes: dict[str, object], reason: ExclusionReason
):
    decision = classify_v1_workspace_usage_row(_workspace_row(**changes))
    assert not decision.eligible
    assert decision.reason is reason


def test_v1_partition_preserves_rows_and_exposes_stable_exclusions():
    workspace = _workspace_row()
    vm = _workspace_row(
        measurement_basis="guest-provisioned",
        resource_class="virtual-machine",
        resource="job_vm",
    )
    selection = partition_v1_workspace_usage([workspace, vm])

    assert selection.eligible_rows == (workspace,)
    assert len(selection.exclusions) == 1
    assert selection.exclusions[0].row_index == 1
    assert selection.exclusions[0].reason is ExclusionReason.MEASUREMENT_BASIS_MISMATCH
