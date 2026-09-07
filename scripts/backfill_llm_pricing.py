#!/usr/bin/env python3
"""Backfill LLM $/token cost onto historical ``usage_events`` rows.

Cost in the "By Model" usage view is a **stored snapshot**: ``UsageLedger`` prices
each token row from ``usage_rates`` once, at INSERT time. A model recorded before a
matching rate existed (e.g. ``gpt-5.6-terra`` / ``gpt-5.6-sol``, whose bare name
did not match the ``openai/…`` OpenRouter id until the suffix resolver landed —
see ``services/openrouter_pricing._build_price_resolver``) was written with
``cost_usd = NULL`` and shows ``—`` forever, because no read path re-consults
``usage_rates``. The forward fix only prices *new* rows; this script re-prices the
*existing* ones.

It is a deliberate, manually-run **one-off** (not wired into any loop). The
ledger remains append-only: each previously unpriced row is offset by a negative
unpriced correction and replaced by an equal positive priced correction. The
net quantity is unchanged, cost becomes visible, and deterministic correction
keys make a re-run a no-op. Per resource it:

  1. Resolves the price the same way the forward sync does — the model catalog's
     ``params_json.pricing_id`` (or the bare id) matched against OpenRouter via
     the shared resolver — so an admin override / force-unprice is honoured.
  2. Seeds a historical ``usage_rates`` row (app DB) effective from the resource's
     earliest event, so the effective-dated rate history is complete.
  3. INSERTs the two additive correction rows in ``usage_events`` (auditdb), per
     unit. The original immutable row is retained as provenance.
  4. After all resources, forces the ``usage_daily`` rollup to re-close the
     affected day span via ``UsageRollup.run_pass`` (a full-replace upsert), so
     the served numbers reflect the corrected ledger — otherwise closed days keep
     serving the old zero from the rollup mirror.

Dry-run by default; pass ``--apply`` to write. Run where the DB env is present
(POSTGRES_* / AUDIT_POSTGRES_* or DATABASE_URL / AUDIT_DB_URL), e.g. inside the
orchestrator pod:

    kubectl --context=k3d-srw -n srw exec deploy/srw-orchestrator -c orchestrator -- \
        python /app/scripts/backfill_llm_pricing.py \
        --resource gpt-5.6-terra --resource gpt-5.6-sol            # dry-run
    ... --resource gpt-5.6-terra --resource gpt-5.6-sol --apply    # write
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Optional


from orchestrator.database.postgres import PostgresDB
from orchestrator.services.openrouter_pricing import (
    _build_price_resolver,
    _pricing_id_for,
    fetch_openrouter_prices,
)
from orchestrator.services.usage_ledger import UsageLedger, UsageRates
from orchestrator.services.usage_rollup import UsageRollup
from shared.db_url import build_postgres_url

logger = logging.getLogger("backfill_llm_pricing")

# Per-unit originals that do not yet have both deterministic correction rows.
_AFFECTED_SQL = """
SELECT unit, COUNT(*) AS n, MIN(ts) AS min_ts, COALESCE(SUM(quantity), 0) AS qty
FROM usage_events AS original
WHERE original.category = 'llm'
  AND original.resource = $1
  AND original.cost_usd IS NULL
  AND original.source <> 'llm-pricing-correction-v1'
  AND original.quantity >= 0
  AND NOT (
      EXISTS (
          SELECT 1
          FROM usage_events AS correction
          WHERE correction.source = 'llm-pricing-correction-v1'
            AND correction.source_id =
                original.id::text || ':unpriced-reversal'
            AND correction.unit = original.unit
            AND correction.ts = original.ts
      )
      AND EXISTS (
          SELECT 1
          FROM usage_events AS correction
          WHERE correction.source = 'llm-pricing-correction-v1'
            AND correction.source_id =
                original.id::text || ':priced-replacement'
            AND correction.unit = original.unit
            AND correction.ts = original.ts
      )
  )
GROUP BY unit
"""

# One statement publishes both sides of every correction atomically. If an old
# interrupted/manual run somehow left one side behind, ON CONFLICT preserves it
# and inserts the missing side.
_CORRECT_SQL = """
WITH originals AS (
    SELECT original.*
    FROM usage_events AS original
    WHERE original.category = 'llm'
      AND original.resource = $1
      AND original.unit = $2
      AND original.cost_usd IS NULL
      AND original.source <> 'llm-pricing-correction-v1'
      AND original.quantity >= 0
), correction_rows AS (
    SELECT
        original.*,
        correction.variant,
        correction.quantity_sign
    FROM originals AS original
    CROSS JOIN (
        VALUES ('unpriced-reversal'::text, -1::numeric),
               ('priced-replacement'::text, 1::numeric)
    ) AS correction(variant, quantity_sign)
)
INSERT INTO usage_events (
    ts, user_id, project_id, ref_kind, ref_id, category, resource,
    quantity, unit, rate_usd, cost_usd, source, source_id, details
)
SELECT
    original.ts,
    original.user_id,
    original.project_id,
    original.ref_kind,
    original.ref_id,
    original.category,
    original.resource,
    original.quantity * original.quantity_sign,
    original.unit,
    CASE WHEN original.quantity_sign > 0 THEN $3::numeric ELSE NULL END,
    CASE
        WHEN original.quantity_sign > 0
        THEN original.quantity * $3::numeric
        ELSE NULL
    END,
    'llm-pricing-correction-v1',
    original.id::text || ':' || original.variant,
    jsonb_build_object(
        'reason', 'historical-llm-pricing-backfill',
        'original_id', original.id,
        'original_source', original.source,
        'original_source_id', original.source_id,
        'variant', original.variant
    )
FROM correction_rows AS original
ON CONFLICT (source, source_id, unit, ts) DO NOTHING
"""

_SEED_RATE_SQL = """
INSERT INTO usage_rates (category, resource, unit, rate_usd, effective_from)
VALUES ('llm', $1, $2, $3, $4)
ON CONFLICT (category, resource, unit, effective_from) DO NOTHING
"""


def _status_count(tag: str) -> int:
    """Parse the row count from an asyncpg command tag like ``UPDATE 42``."""
    try:
        return int(tag.split()[-1])
    except (ValueError, IndexError):
        return 0


async def _connect_pools() -> tuple[PostgresDB, PostgresDB]:
    """Open the app pool + the auditdb pool the same way the orchestrator does."""
    app_db = PostgresDB()
    await app_db.connect()

    audit_url = build_postgres_url("AUDIT_POSTGRES", fallback_env="AUDIT_DB_URL")
    if not audit_url:
        await app_db.close()
        raise SystemExit(
            "AUDIT DB credentials missing (AUDIT_POSTGRES_* / AUDIT_DB_URL) — "
            "usage_events lives in the auditdb; cannot backfill without it."
        )
    audit_db = PostgresDB(
        connection_string=audit_url,
        env_prefix="AUDIT_POSTGRES",
        default_min_connections=1,
        default_max_connections=4,
    )
    await audit_db.connect()
    return app_db, audit_db


async def _pricing_id_map(app_db: PostgresDB) -> dict[str, Optional[str]]:
    """``{model_id: params_json.pricing_id}`` from the catalog (mirrors the sync)."""
    out: dict[str, Optional[str]] = {}
    for m in await app_db.list_models():
        mid = m.get("model_id")
        if mid:
            out[str(mid)] = (m.get("params_json") or {}).get("pricing_id")
    return out


async def backfill(resources: list[str], *, apply: bool) -> int:
    """Backfill cost for ``resources``. Returns process exit code."""
    app_db, audit_db = await _connect_pools()
    app_pool, audit_pool = app_db.pool, audit_db.pool
    try:
        prices = await fetch_openrouter_prices()
        if not prices:
            logger.error("OpenRouter catalog fetch returned nothing — aborting.")
            return 1
        resolve = _build_price_resolver(prices)
        pid_map = await _pricing_id_map(app_db)

        overall_earliest: Optional[datetime] = None
        any_applied = False

        for resource in resources:
            candidate = _pricing_id_for(resource, pid_map.get(resource))
            price = resolve(candidate)
            if price is None:
                logger.warning(
                    "[%s] no OpenRouter price resolved (candidate=%r) — skipping. "
                    "Set params_json.pricing_id in Admin → Models if it must price.",
                    resource,
                    candidate,
                )
                continue
            rates = price.rates()  # unit -> Decimal $/token

            async with audit_pool.acquire() as conn:
                affected = await conn.fetch(_AFFECTED_SQL, resource)
            affected = [r for r in affected if r["unit"] in rates]
            if not affected:
                logger.info("[%s] no NULL-cost rows to backfill — skipping.", resource)
                continue

            res_min_ts: datetime = min(r["min_ts"] for r in affected)
            if overall_earliest is None or res_min_ts < overall_earliest:
                overall_earliest = res_min_ts
            total_rows = sum(r["n"] for r in affected)
            est_cost = sum(Decimal(str(r["qty"])) * rates[r["unit"]] for r in affected)
            logger.info(
                "[%s] %d row(s) since %s, est +$%.4f across units: %s",
                resource,
                total_rows,
                res_min_ts.date(),
                est_cost,
                ", ".join(f"{r['unit']}={r['n']}@{rates[r['unit']]}" for r in affected),
            )
            if not apply:
                continue

            # 2) Seed historical rate; 3) publish additive correction pairs.
            async with app_pool.acquire() as conn:
                for r in affected:
                    await conn.execute(
                        _SEED_RATE_SQL,
                        resource,
                        r["unit"],
                        rates[r["unit"]],
                        res_min_ts,
                    )
            async with audit_pool.acquire() as conn:
                for r in affected:
                    rate = rates[r["unit"]]
                    tag = await conn.execute(_CORRECT_SQL, resource, r["unit"], rate)
                    logger.info(
                        "[%s] inserted %d %s correction row(s) @ %s",
                        resource,
                        _status_count(tag),
                        r["unit"],
                        rate,
                    )
            any_applied = True

        if not apply:
            logger.info("Dry run — no writes. Re-run with --apply to backfill.")
            return 0
        if not any_applied:
            logger.info("Nothing applied.")
            return 0

        # 4) Re-close the affected day span so served (closed-day) numbers reflect
        # the corrected ledger. run_pass is a full-replace upsert → idempotent.
        await _recompute_rollup(app_pool, audit_pool, overall_earliest)
        logger.info("Backfill complete.")
        return 0
    finally:
        await audit_db.close()
        await app_db.close()


async def _recompute_rollup(app_pool, audit_pool, earliest: Optional[datetime]) -> None:
    """Force ``usage_daily`` to re-aggregate from ``earliest`` forward, if needed."""
    if earliest is None:
        return
    watermark: Optional[date] = await app_pool.fetchval(
        "SELECT last_closed_day FROM rollup_state WHERE name = 'usage_daily'"
    )
    earliest_day = earliest.date()
    if watermark is None or earliest_day > watermark:
        # Affected days are still in the open tail — served live from the raw
        # (now-corrected) ledger, so no rollup rewrite is needed.
        logger.info(
            "Rollup: affected days are in the open tail (watermark=%s) — "
            "no re-close needed.",
            watermark,
        )
        return
    trailing = (watermark - earliest_day).days + 1
    ledger = UsageLedger(audit_pool, UsageRates(app_pool))
    rollup = UsageRollup(audit_pool, app_pool, ledger)
    result = await rollup.run_pass(trailing_days=trailing)
    logger.info(
        "Rollup: re-closed %d trailing day(s) back to %s (%s).",
        trailing,
        earliest_day,
        result,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--resource",
        action="append",
        required=True,
        metavar="MODEL_ID",
        help="usage_events.resource (== recorded model id) to backfill; repeatable.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Perform the writes. Omit for a dry-run report (the default).",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    raise SystemExit(asyncio.run(backfill(args.resource, apply=args.apply)))


if __name__ == "__main__":
    main()
