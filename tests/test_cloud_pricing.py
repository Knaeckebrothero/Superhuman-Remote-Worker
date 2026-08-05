"""Cloud-equivalent compute rate parsing and estimation tests."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from orchestrator.services.cloud_pricing import (
    CloudCostEstimator,
    ProviderRate,
    _sync_card_rates,
    parse_aws_fargate_rates,
    parse_azure_aci_rates,
)


def _aws_payload() -> dict:
    products = {
        "cpu": {
            "attributes": {
                "regionCode": "eu-central-1",
                "usagetype": "EUC1-Fargate-vCPU-Hours:perCPU",
            }
        },
        "memory": {
            "attributes": {
                "regionCode": "eu-central-1",
                "usagetype": "EUC1-Fargate-GB-Hours",
            }
        },
        # Exact selectors must ignore a tempting Windows price.
        "windows": {
            "attributes": {
                "regionCode": "eu-central-1",
                "usagetype": "EUC1-Fargate-Windows-vCPU-Hours:perCPU",
                "operatingSystem": "Windows",
            }
        },
    }

    def term(description: str, value: str) -> dict:
        return {
            "offer": {
                "priceDimensions": {
                    "dimension": {
                        "description": description,
                        "unit": "hours",
                        "pricePerUnit": {"USD": value},
                    }
                }
            }
        }

    return {
        "products": products,
        "terms": {
            "OnDemand": {
                "cpu": term("AWS Fargate - vCPU - EU (Frankfurt)", "0.04656"),
                "memory": term("AWS Fargate - Memory - EU (Frankfurt)", "0.00511"),
                "windows": term("Windows", "0.053544"),
            }
        },
    }


def _azure_payload() -> dict:
    def item(meter: str, price: float) -> dict:
        return {
            "meterName": meter,
            "meterId": meter.lower().replace(" ", "-"),
            "skuName": "Standard",
            "armRegionName": "germanywestcentral",
            "type": "Consumption",
            "currencyCode": "USD",
            "retailPrice": price,
            "unitOfMeasure": "1 Hour",
            "effectiveStartDate": "2026-01-01T00:00:00Z",
        }

    return {
        "Items": [
            item("Standard vCPU Duration", 0.04656),
            item("Standard Memory Duration", 0.00511),
            {
                **item("Confidential containers ACI vCPU Duration", 99),
                "skuName": "Confidential containers ACI",
            },
        ]
    }


def test_parses_exact_aws_fargate_linux_x86_pair():
    rates = parse_aws_fargate_rates(_aws_payload())
    assert rates["vcpu-hour"].rate == Decimal("0.04656")
    assert rates["gib-hour"].rate == Decimal("0.00511")
    assert rates["vcpu-hour"].source_sku == "EUC1-Fargate-vCPU-Hours:perCPU"


def test_parses_exact_azure_aci_standard_pair():
    rates = parse_azure_aci_rates(_azure_payload())
    assert rates["vcpu-hour"].rate == Decimal("0.04656")
    assert rates["gib-hour"].rate == Decimal("0.00511")
    assert rates["gib-hour"].source_sku == "Standard Memory Duration"


def test_incomplete_provider_pair_fails_closed():
    aws = _aws_payload()
    del aws["products"]["memory"]
    assert parse_aws_fargate_rates(aws) == {}
    assert parse_azure_aci_rates({"Items": _azure_payload()["Items"][:1]}) == {}


class _AsyncContext:
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class FakeSyncConn:
    def __init__(self, current: dict[tuple[str, str], Decimal]):
        self.current = current
        self.inserts: list[tuple[str, str, Decimal]] = []
        self.checked: list[tuple[str, datetime]] = []

    def transaction(self):
        return _AsyncContext()

    async def fetchval(self, sql: str, *args):
        if "SELECT EXISTS" in sql:
            return True
        return self.current.get((args[0], args[1]))

    async def execute(self, sql: str, *args):
        if sql.startswith("INSERT"):
            card_id, unit, rate = args[:3]
            self.inserts.append((card_id, unit, rate))
            self.current[(card_id, unit)] = rate
        else:
            self.checked.append((args[0], args[1]))


class FakeSyncPool:
    def __init__(self, conn: FakeSyncConn):
        self.conn = conn

    def acquire(self):
        return _AsyncContext(self.conn)


@pytest.mark.asyncio
async def test_rate_sync_is_change_only_but_records_successful_source_check():
    card_id = "aws-fargate-euc1"
    checked = datetime(2026, 8, 5, tzinfo=timezone.utc)
    conn = FakeSyncConn(
        {
            (card_id, "vcpu-hour"): Decimal("0.04656"),
            (card_id, "gib-hour"): Decimal("0.00500"),
        }
    )
    rates = {
        "vcpu-hour": ProviderRate(Decimal("0.04656"), "cpu", {}),
        "gib-hour": ProviderRate(Decimal("0.00511"), "memory", {}),
    }
    inserted = await _sync_card_rates(FakeSyncPool(conn), card_id, rates, checked)
    assert inserted == 1
    assert conn.inserts == [(card_id, "gib-hour", Decimal("0.00511"))]
    assert conn.checked == [(card_id, checked)]


class FakeReadPool:
    def __init__(self, cards: list[dict], rates: list[dict]):
        self.cards = cards
        self.rates = rates

    async def fetch(self, sql: str):
        return self.cards if "FROM usage_rate_cards WHERE" in sql else self.rates


@pytest.mark.asyncio
async def test_estimates_linear_and_bundled_node_share_without_touching_llm_cost():
    effective = datetime(2026, 8, 4, tzinfo=timezone.utc)
    checked = datetime(2026, 8, 5, tzinfo=timezone.utc)

    def card(card_id: str, aggregation: str, currency: str) -> dict:
        return {
            "id": card_id,
            "provider": card_id,
            "display_name": card_id,
            "region": "test-region",
            "currency": currency,
            "aggregation": aggregation,
            "source_url": "https://example.test/prices",
            "source_label": "test prices",
            "description": "test",
            "exclusions": "test exclusions",
            "source_checked_at": checked,
            "sort_order": 1,
        }

    def rate(card_id: str, unit: str, value: str, capacity: str) -> dict:
        return {
            "rate_card_id": card_id,
            "category": "compute",
            "resource": "*",
            "unit": unit,
            "rate": Decimal(value),
            "capacity_per_billing_unit": Decimal(capacity),
            "effective_from": effective,
            "source_sku": unit,
        }

    pool = FakeReadPool(
        [card("linear", "sum", "USD"), card("node", "max", "EUR")],
        [
            rate("linear", "vcpu-hour", "0.04656", "1"),
            rate("linear", "gib-hour", "0.00511", "1"),
            rate("node", "vcpu-hour", "0.20458503352", "4"),
            rate("node", "gib-hour", "0.20458503352", "16"),
        ],
    )
    estimator = CloudCostEstimator(pool)
    result = await estimator.estimate(
        [
            {"category": "compute", "unit": "vcpu-hour", "quantity": 2},
            {"category": "compute", "unit": "gib-hour", "quantity": 8},
            # LLM quantities have no card component and cannot leak into compute.
            {"category": "llm", "unit": "completion-token", "quantity": 9_000_000},
        ],
        as_of=checked,
    )

    by_id = {row["id"]: row for row in result}
    assert by_id["linear"]["estimate"] == pytest.approx(0.134)
    # Both requested shares are 1/2 node; max, not sum, prices a half node-hour.
    assert by_id["node"]["estimate"] == pytest.approx(0.10229251676)
    assert by_id["node"]["currency"] == "EUR"
    assert len(by_id["node"]["components"]) == 2


@pytest.mark.asyncio
async def test_estimator_uses_newest_effective_rate_only():
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    new = datetime(2026, 7, 1, tzinfo=timezone.utc)
    card = {
        "id": "linear",
        "provider": "test",
        "display_name": "test",
        "region": "test",
        "currency": "USD",
        "aggregation": "sum",
        "source_url": "https://example.test",
        "source_label": "test",
        "description": "",
        "exclusions": "",
        "source_checked_at": new,
        "sort_order": 1,
    }
    base = {
        "rate_card_id": "linear",
        "category": "compute",
        "resource": "*",
        "unit": "vcpu-hour",
        "capacity_per_billing_unit": Decimal(1),
        "source_sku": "cpu",
    }
    pool = FakeReadPool(
        [card],
        [
            {**base, "rate": Decimal("1"), "effective_from": old},
            {**base, "rate": Decimal("2"), "effective_from": new},
        ],
    )
    result = await CloudCostEstimator(pool).estimate(
        [{"category": "compute", "unit": "vcpu-hour", "quantity": 3}],
        as_of=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    assert result[0]["estimate"] == 6


@pytest.mark.asyncio
async def test_estimator_returns_no_cards_when_window_has_no_compute():
    estimator = CloudCostEstimator(FakeReadPool([], []))
    result = await estimator.estimate(
        [{"category": "llm", "unit": "completion-token", "quantity": 10}]
    )
    assert result == []


@pytest.mark.asyncio
async def test_estimator_wildcard_prices_only_typed_workspace_pod_rows():
    now = datetime(2026, 8, 5, tzinfo=timezone.utc)
    card = {
        "id": "legacy-wildcard",
        "provider": "test",
        "display_name": "test",
        "region": "test",
        "currency": "USD",
        "aggregation": "sum",
        "source_url": "https://example.test",
        "source_label": "test",
        "description": "",
        "exclusions": "",
        "source_checked_at": now,
        "sort_order": 1,
    }
    rate = {
        "rate_card_id": "legacy-wildcard",
        "category": "compute",
        "resource": "*",
        "unit": "vcpu-hour",
        "rate": Decimal("1"),
        "capacity_per_billing_unit": Decimal("1"),
        "effective_from": now,
        "source_sku": "cpu",
    }

    def typed_row(**changes):
        row = {
            "category": "compute",
            "measurement_basis": "scheduler-request",
            "cost_domain": "workload-allocation",
            "resource_class": "kubernetes-pod",
            "resource": "workspace_pod",
            "unit": "vcpu-hour",
            "attribution_scope": "customer",
            "quantity": 2,
        }
        row.update(changes)
        return row

    result = await CloudCostEstimator(FakeReadPool([card], [rate])).estimate(
        [
            typed_row(),
            typed_row(resource="agent_pod", quantity=100),
            typed_row(
                measurement_basis="guest-provisioned",
                resource_class="virtual-machine",
                resource="job_vm",
                quantity=100,
            ),
            typed_row(attribution_scope="shared-platform", quantity=100),
            typed_row(
                category="storage",
                measurement_basis="claim-requested",
                resource_class="persistent-volume-claim",
                resource="workspace_pvc",
                unit="gib-hour",
                quantity=100,
            ),
            # Once any typed dimension is present, incomplete rows fail closed.
            {
                "category": "compute",
                "resource": "workspace_pod",
                "unit": "vcpu-hour",
                "quantity": 100,
            },
        ],
        as_of=now,
    )

    assert result[0]["estimate"] == 2
    assert result[0]["components"][0]["quantity"] == 2
