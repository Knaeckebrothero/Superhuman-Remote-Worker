"""Public-cloud list-price rate cards for usage-ledger compute estimates.

This module deliberately does *not* write ``usage_events.cost_usd``. The ledger's
cost is the immutable canonical charge (for example OpenRouter token spend), while
these cards reprice already-aggregated CPU/RAM quantities for planning.

AWS and Azure expose machine-readable public prices and are refreshed
change-only. STACKIT's public price list is a PDF, so its current g2i.4 reference
rate is seeded by migration and source-labelled instead of scraped at runtime.
Every path is non-load-bearing: missing tables, network errors, or malformed
provider data produce no estimate/update and never interrupt metering.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

import asyncpg
import httpx

from orchestrator.services.cloud_pricing_contracts import partition_v1_workspace_usage

logger = logging.getLogger(__name__)

AWS_CARD_ID = "aws-fargate-euc1"
AWS_PRICE_URL = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/"
    "AmazonECS/current/eu-central-1/index.json"
)
AZURE_CARD_ID = "azure-aci-dewc"
AZURE_PRICE_URL = "https://prices.azure.com/api/retail/prices"
AZURE_FILTER = (
    "serviceName eq 'Container Instances' and "
    "armRegionName eq 'germanywestcentral' and "
    "priceType eq 'Consumption'"
)


def _decimal(value: Any) -> Optional[Decimal]:
    """Return a finite non-negative Decimal, otherwise ``None``."""
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _number(value: Decimal) -> float:
    """JSON-friendly numeric output at the API boundary."""
    return float(value)


@dataclass(frozen=True)
class ProviderRate:
    rate: Decimal
    source_sku: str
    source_metadata: dict[str, Any]


def parse_aws_fargate_rates(payload: dict[str, Any]) -> dict[str, ProviderRate]:
    """Extract Frankfurt Linux/x86 Fargate CPU and memory prices.

    The public Amazon ECS offer file does not label its default Fargate rows as
    Linux/x86; that is represented by the absence of the Windows/ARM attributes.
    Exact usage-type matching avoids accidentally selecting Spot, Windows, ARM,
    or ephemeral-storage rates.
    """
    wanted = {
        "EUC1-Fargate-vCPU-Hours:perCPU": "vcpu-hour",
        "EUC1-Fargate-GB-Hours": "gib-hour",
    }
    out: dict[str, ProviderRate] = {}
    products = payload.get("products") or {}
    terms = (payload.get("terms") or {}).get("OnDemand") or {}
    for sku, product in products.items():
        attrs = (product or {}).get("attributes") or {}
        unit = wanted.get(attrs.get("usagetype"))
        if unit is None or attrs.get("regionCode") != "eu-central-1":
            continue
        if attrs.get("operatingSystem") or attrs.get("cpuArchitecture"):
            continue
        dimensions = []
        for term in (terms.get(sku) or {}).values():
            dimensions.extend((term.get("priceDimensions") or {}).values())
        if len(dimensions) != 1:
            continue
        dimension = dimensions[0]
        rate = _decimal((dimension.get("pricePerUnit") or {}).get("USD"))
        if rate is None:
            continue
        out[unit] = ProviderRate(
            rate=rate,
            source_sku=str(attrs.get("usagetype") or sku),
            source_metadata={
                "offer_sku": str(sku),
                "description": str(dimension.get("description") or ""),
                "unit": str(dimension.get("unit") or ""),
            },
        )
    return out if set(out) == {"vcpu-hour", "gib-hour"} else {}


def parse_azure_aci_rates(payload: dict[str, Any]) -> dict[str, ProviderRate]:
    """Extract Germany West Central Standard ACI CPU and memory prices."""
    wanted = {
        "Standard vCPU Duration": "vcpu-hour",
        "Standard Memory Duration": "gib-hour",
    }
    out: dict[str, ProviderRate] = {}
    for item in payload.get("Items") or []:
        unit = wanted.get(item.get("meterName"))
        if (
            unit is None
            or item.get("skuName") != "Standard"
            or item.get("armRegionName") != "germanywestcentral"
            or item.get("type") != "Consumption"
            or item.get("currencyCode") != "USD"
        ):
            continue
        rate = _decimal(item.get("retailPrice"))
        if rate is None:
            continue
        out[unit] = ProviderRate(
            rate=rate,
            source_sku=str(item.get("meterName")),
            source_metadata={
                "meter_id": str(item.get("meterId") or ""),
                "effective_start": str(item.get("effectiveStartDate") or ""),
                "unit": str(item.get("unitOfMeasure") or ""),
            },
        )
    return out if set(out) == {"vcpu-hour", "gib-hour"} else {}


async def fetch_aws_fargate_rates(
    client: httpx.AsyncClient,
) -> dict[str, ProviderRate]:
    response = await client.get(AWS_PRICE_URL)
    response.raise_for_status()
    return parse_aws_fargate_rates(response.json())


async def fetch_azure_aci_rates(
    client: httpx.AsyncClient,
) -> dict[str, ProviderRate]:
    response = await client.get(
        AZURE_PRICE_URL,
        params={"currencyCode": "'USD'", "$filter": AZURE_FILTER},
    )
    response.raise_for_status()
    return parse_azure_aci_rates(response.json())


async def _sync_card_rates(
    pool: asyncpg.Pool,
    card_id: str,
    rates: dict[str, ProviderRate],
    checked_at: datetime,
) -> int:
    """Insert changed wildcard compute rates and mark a successful source check."""
    if set(rates) != {"vcpu-hour", "gib-hour"}:
        return 0
    inserted = 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            exists = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM usage_rate_cards WHERE id = $1)",
                card_id,
            )
            if not exists:
                return 0
            for unit, provider_rate in rates.items():
                current = await conn.fetchval(
                    "SELECT rate FROM usage_rate_card_rates "
                    "WHERE rate_card_id = $1 AND category = 'compute' "
                    "AND resource = '*' AND unit = $2 "
                    "ORDER BY effective_from DESC LIMIT 1",
                    card_id,
                    unit,
                )
                if current is not None and Decimal(current) == provider_rate.rate:
                    continue
                await conn.execute(
                    "INSERT INTO usage_rate_card_rates "
                    "(rate_card_id, category, resource, unit, rate, "
                    " capacity_per_billing_unit, effective_from, source_sku, "
                    " source_metadata) "
                    "VALUES ($1, 'compute', '*', $2, $3, 1, $4, $5, $6::jsonb) "
                    "ON CONFLICT "
                    "(rate_card_id, category, resource, unit, effective_from) "
                    "DO NOTHING",
                    card_id,
                    unit,
                    provider_rate.rate,
                    checked_at,
                    provider_rate.source_sku,
                    json.dumps(provider_rate.source_metadata),
                )
                inserted += 1
            await conn.execute(
                "UPDATE usage_rate_cards "
                "SET source_checked_at = $2, updated_at = now() WHERE id = $1",
                card_id,
                checked_at,
            )
    return inserted


async def sync_cloud_rates_once(
    app_pool: Optional[asyncpg.Pool],
    *,
    client: Optional[httpx.AsyncClient] = None,
    now: Optional[datetime] = None,
) -> int:
    """Refresh AWS/Azure rate cards once; return inserted effective-rate rows."""
    if app_pool is None:
        return 0
    checked_at = now or datetime.now(timezone.utc)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    inserted = 0
    try:
        fetches = (
            (AWS_CARD_ID, fetch_aws_fargate_rates),
            (AZURE_CARD_ID, fetch_azure_aci_rates),
        )
        results = await asyncio.gather(
            *(fetch(client) for _, fetch in fetches), return_exceptions=True
        )
        for (card_id, _), result in zip(fetches, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "cloud pricing fetch failed for %s (non-fatal)",
                    card_id,
                    exc_info=(type(result), result, result.__traceback__),
                )
                continue
            if not result:
                logger.warning(
                    "cloud pricing source returned no complete rate pair for %s",
                    card_id,
                )
                continue
            inserted += await _sync_card_rates(app_pool, card_id, result, checked_at)
    except Exception:
        logger.warning("cloud pricing sync failed (non-fatal)", exc_info=True)
    finally:
        if owns_client:
            await client.aclose()
    if inserted:
        logger.info("cloud pricing: inserted %d changed rate row(s)", inserted)
    return inserted


async def cloud_pricing_sync_loop(
    shutdown_event: asyncio.Event,
    app_pool: Optional[asyncpg.Pool],
    *,
    interval: float = 86400.0,
) -> None:
    """Refresh public provider rates on entry and daily, non-fatally."""
    if app_pool is None:
        logger.info("Cloud pricing sync loop disabled (app pool unavailable)")
        return
    logger.info("Cloud pricing sync loop starting (interval=%ss)", interval)
    try:
        while not shutdown_event.is_set():
            await sync_cloud_rates_once(app_pool)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("Cloud pricing sync loop stopped")


class CloudCostEstimator:
    """Cached read/estimate surface over effective-dated cloud rate cards."""

    def __init__(self, app_pool: Optional[asyncpg.Pool], *, ttl_s: float = 300.0):
        self._pool = app_pool
        self._ttl = ttl_s
        self._cards: list[dict[str, Any]] = []
        self._rates: list[dict[str, Any]] = []
        self._loaded_at = -1.0

    async def _ensure_loaded(self) -> bool:
        if self._pool is None:
            return False
        now = time.monotonic()
        if self._loaded_at >= 0 and (now - self._loaded_at) < self._ttl:
            return True
        try:
            cards, rates = await asyncio.gather(
                self._pool.fetch(
                    "SELECT id, provider, display_name, region, currency, "
                    "aggregation, source_url, source_label, description, "
                    "exclusions, source_checked_at, sort_order "
                    "FROM usage_rate_cards WHERE enabled = TRUE "
                    "ORDER BY sort_order, id"
                ),
                self._pool.fetch(
                    "SELECT rate_card_id, category, resource, unit, rate, "
                    "capacity_per_billing_unit, effective_from, source_sku "
                    "FROM usage_rate_card_rates"
                ),
            )
            self._cards = [dict(row) for row in cards]
            self._rates = [dict(row) for row in rates]
            self._loaded_at = now
            return True
        except Exception:
            logger.warning(
                "cloud rate cards load failed; estimates unavailable",
                exc_info=True,
            )
            # Keep serving the last complete list-price snapshot across a
            # transient app-DB read failure; initial load still returns false.
            return bool(self._cards and self._rates)

    async def estimate(
        self,
        usage_rows: Iterable[dict[str, Any]],
        *,
        as_of: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Reprice aggregated ``(category, unit, quantity)`` ledger rows.

        `/api/usage` intentionally aggregates away the resource string, so the
        first slice consumes only wildcard (`resource='*'`) rate components.
        Specific-resource cards can be supported when the API carries resource
        through its aggregate shape.
        """
        if not await self._ensure_loaded():
            return []
        priced_at = as_of or datetime.now(timezone.utc)
        quantities: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
        # Legacy cards deliberately wildcard ``resource``. Keep their original
        # workspace Pod scope as typed v2 VM, agent, platform, and storage rows
        # begin sharing this estimator input. Partial typed rows fail closed.
        selection = partition_v1_workspace_usage(usage_rows)
        for row in selection.eligible_rows:
            quantity = _decimal(row.get("quantity"))
            category = row.get("category")
            unit = row.get("unit")
            if quantity is not None and category and unit:
                quantities[(str(category), str(unit))] += quantity
        if not any(
            category == "compute" and quantity > 0
            for (category, _), quantity in quantities.items()
        ):
            return []

        out: list[dict[str, Any]] = []
        for card in self._cards:
            candidates = [
                rate
                for rate in self._rates
                if rate["rate_card_id"] == card["id"]
                and rate["resource"] == "*"
                and rate["effective_from"] <= priced_at
            ]
            newest: dict[tuple[str, str], dict[str, Any]] = {}
            for rate in candidates:
                key = (rate["category"], rate["unit"])
                if key not in newest or (
                    rate["effective_from"] > newest[key]["effective_from"]
                ):
                    newest[key] = rate

            components: list[dict[str, Any]] = []
            amounts: list[Decimal] = []
            for (category, unit), rate_row in sorted(newest.items()):
                quantity = quantities[(category, unit)]
                rate = Decimal(rate_row["rate"])
                capacity = Decimal(rate_row["capacity_per_billing_unit"])
                amount = quantity / capacity * rate
                amounts.append(amount)
                components.append(
                    {
                        "category": category,
                        "unit": unit,
                        "quantity": _number(quantity),
                        "rate": _number(rate),
                        "capacity_per_billing_unit": _number(capacity),
                        "amount": _number(amount),
                        "source_sku": rate_row.get("source_sku"),
                        "effective_from": rate_row["effective_from"].isoformat(),
                    }
                )
            aggregation = card["aggregation"]
            estimate = (
                (max(amounts) if aggregation == "max" else sum(amounts, Decimal()))
                if amounts
                else Decimal()
            )
            checked = card.get("source_checked_at")
            out.append(
                {
                    "id": card["id"],
                    "provider": card["provider"],
                    "display_name": card["display_name"],
                    "region": card["region"],
                    "currency": card["currency"],
                    "aggregation": aggregation,
                    "estimate": _number(estimate),
                    "priced_at": priced_at.isoformat(),
                    "source_url": card["source_url"],
                    "source_label": card["source_label"],
                    "source_checked_at": checked.isoformat() if checked else None,
                    "description": card["description"],
                    "exclusions": card["exclusions"],
                    "components": components,
                }
            )
        return out


__all__ = [
    "AWS_CARD_ID",
    "AWS_PRICE_URL",
    "AZURE_CARD_ID",
    "AZURE_PRICE_URL",
    "CloudCostEstimator",
    "ProviderRate",
    "cloud_pricing_sync_loop",
    "fetch_aws_fargate_rates",
    "fetch_azure_aci_rates",
    "parse_aws_fargate_rates",
    "parse_azure_aci_rates",
    "sync_cloud_rates_once",
]
