"""OpenRouter model-pricing sync → ``usage_rates`` (LLM $/token rates).

Replaces LiteLLM's per-request ``spend`` as the cost source now that the gateway
is gone (``docs/issues/remove_litellm_proxy_and_gateway_concept.md``, P1). The
in-process metering path materializes cost-free ``prompt-token`` / ``completion
-token`` rows into ``usage_events`` (from the ``llm_requests`` audit); this module
seeds the effective-dated ``usage_rates`` table so
:meth:`UsageLedger.record_events` prices those rows exactly as it priced the
gateway spend log before.

Source: OpenRouter's public model catalog (``openrouter.ai/api/v1/models`` — no
auth), which publishes per-model ``pricing.prompt`` / ``pricing.completion`` as
USD-*per-token* decimal strings.

Mapping: each catalog model names the OpenRouter id to price against via
``params_json.pricing_id`` (admin-set, Admin → Models). ``pricing_id = ""`` marks
a model explicitly unpriced (self-hosted / free) → no rate → ``cost_usd`` stays
NULL. When unset we fall back to a light normalization of the model id; unmatched
models are left unpriced rather than mis-priced.

Effective-dated + change-only: a sync inserts a NEW ``usage_rates`` row only when
the upstream price differs from the current newest rate for that (resource, unit),
so re-running every N minutes never bloats the effective-dated table and never
rewrites already-snapshotted history. Non-fatal throughout (network/DB errors log
and no-op) — same graceful-degradation posture as the rest of the metering tier.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

import asyncpg
import httpx

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# The two LLM token dimensions we price. These exact unit strings are what
# ``usage_events`` / ``usage_rates`` key on. ``fetch_openrouter_prices`` returns
# each model's (prompt, completion) $/token in this same order, so a positional
# zip aligns unit ↔ rate.
_LLM_TOKEN_UNITS = ("prompt-token", "completion-token")


def _price(v: Any) -> Optional[Decimal]:
    """Parse an OpenRouter price value to a Decimal $/token.

    OpenRouter gives decimal strings in USD per token (e.g. ``"0.0000015"`` =
    $1.50/M). Returns ``None`` for absent / non-numeric / negative values (a
    negative such as ``"-1"`` signals variable/BYO pricing → treat as unpriced).
    ``"0"`` (free model) is a valid, priced rate of 0.
    """
    if v is None:
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d >= 0 else None


def _pricing_id_for(resource: str, pricing_id: Optional[str]) -> Optional[str]:
    """Resolve the OpenRouter id to price ``resource`` against.

    Prefers the admin-set ``params_json.pricing_id``. An explicit empty string
    means "unpriced on purpose" (self-hosted / free) → ``None``. When unset, fall
    back to a lowercased normalization of the model id as a best-effort match;
    admins should set ``pricing_id`` for anything that must be priced exactly.
    """
    if pricing_id is not None:
        pid = pricing_id.strip()
        return pid or None  # "" → explicitly unpriced
    norm = resource.strip().lower()
    return norm or None


async def fetch_openrouter_prices(
    *, client: Optional[httpx.AsyncClient] = None, timeout: float = 15.0
) -> dict[str, tuple[Decimal, Decimal]]:
    """Fetch ``{openrouter_model_id: (prompt_$per_token, completion_$per_token)}``.

    Public endpoint (no key). Models missing either price are skipped. Non-fatal:
    any network/parse error logs and returns ``{}`` (→ sync no-ops, cost stays as
    last snapshotted).
    """
    owns = client is None
    client = client or httpx.AsyncClient(timeout=timeout)
    try:
        try:
            resp = await client.get(OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            data = resp.json().get("data") or []
        except Exception:
            logger.warning("openrouter pricing fetch failed (non-fatal)", exc_info=True)
            return {}
    finally:
        if owns:
            await client.aclose()

    out: dict[str, tuple[Decimal, Decimal]] = {}
    for m in data:
        mid = m.get("id")
        pricing = m.get("pricing") or {}
        prompt = _price(pricing.get("prompt"))
        completion = _price(pricing.get("completion"))
        if mid and prompt is not None and completion is not None:
            out[str(mid)] = (prompt, completion)
    return out


async def _rate_changed(
    conn: asyncpg.Connection, resource: str, unit: str, rate: Decimal
) -> bool:
    """True if ``rate`` differs from the newest ``llm`` rate for (resource, unit)."""
    cur = await conn.fetchval(
        "SELECT rate_usd FROM usage_rates "
        "WHERE category = 'llm' AND resource = $1 AND unit = $2 "
        "ORDER BY effective_from DESC LIMIT 1",
        resource,
        unit,
    )
    return cur is None or Decimal(cur) != rate


async def sync_llm_rates(
    app_pool: Optional[asyncpg.Pool],
    models: Iterable[tuple[str, Optional[str]]],
    *,
    prices: Optional[dict[str, tuple[Decimal, Decimal]]] = None,
    now: Optional[datetime] = None,
) -> int:
    """Seed ``usage_rates`` LLM $/token rows for ``models`` from OpenRouter pricing.

    ``models`` = ``(resource, pricing_id)`` pairs, where ``resource`` is the exact
    string that appears in ``usage_events.resource`` (== the recorded
    ``llm_requests.model``) and ``pricing_id`` is the admin-set OpenRouter id (or
    ``None`` → heuristic). Inserts a new effective-dated row only when the price
    changed (see module docstring). Returns the count of rate rows inserted.
    Non-fatal: returns 0 when the app pool is unavailable.
    """
    if app_pool is None:
        return 0
    if prices is None:
        prices = await fetch_openrouter_prices()
    if not prices:
        return 0
    ts = now or datetime.now(timezone.utc)
    inserted = 0
    async with app_pool.acquire() as conn:
        for resource, pricing_id in models:
            pid = _pricing_id_for(resource, pricing_id)
            price = prices.get(pid) if pid else None
            if price is None:
                continue  # unmatched / self-hosted → leave unpriced (cost = NULL)
            for unit, rate in zip(_LLM_TOKEN_UNITS, price):
                if await _rate_changed(conn, resource, unit, rate):
                    await conn.execute(
                        "INSERT INTO usage_rates "
                        "(category, resource, unit, rate_usd, effective_from) "
                        "VALUES ('llm', $1, $2, $3, $4) "
                        "ON CONFLICT (category, resource, unit, effective_from) "
                        "DO NOTHING",
                        resource,
                        unit,
                        rate,
                        ts,
                    )
                    inserted += 1
    if inserted:
        logger.info("openrouter pricing: inserted %d new usage_rates row(s)", inserted)
    return inserted


__all__ = ["fetch_openrouter_prices", "sync_llm_rates", "OPENROUTER_MODELS_URL"]
