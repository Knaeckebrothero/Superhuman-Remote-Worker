"""Unit tests for the OpenRouter → usage_rates pricing sync.

Covers price parsing, the model→OpenRouter-id mapping, the public-catalog fetch
(via httpx.MockTransport — no network), and the effective-dated *change-only*
upsert into usage_rates (via a fake asyncpg pool/conn).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from orchestrator.services.openrouter_pricing import (
    _price,
    _pricing_id_for,
    fetch_openrouter_prices,
    sync_llm_rates,
)


# --- fake asyncpg pool/conn -------------------------------------------------


class FakeConn:
    """Records inserts; answers _rate_changed's fetchval from in-memory state."""

    def __init__(self, existing=None):
        # {(resource, unit): Decimal}
        self.existing = dict(existing or {})
        self.inserts: list[tuple[str, str, Decimal]] = []

    async def fetchval(self, _sql, resource, unit):
        return self.existing.get((resource, unit))

    async def execute(self, _sql, resource, unit, rate, _ts):
        self.inserts.append((resource, unit, rate))
        self.existing[(resource, unit)] = rate  # reflect the insert for change-only


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_a):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- _price -----------------------------------------------------------------


class TestPrice:
    def test_valid_decimal_string(self):
        assert _price("0.0000015") == Decimal("0.0000015")

    def test_zero_is_priced(self):
        # A free model prices at 0 (not "unpriced").
        assert _price("0") == Decimal("0")

    def test_negative_is_variable_unpriced(self):
        assert _price("-1") is None

    def test_none_and_garbage(self):
        assert _price(None) is None
        assert _price("abc") is None


# --- _pricing_id_for --------------------------------------------------------


class TestPricingIdFor:
    def test_admin_pricing_id_wins(self):
        assert (
            _pricing_id_for("MiniMax-M3", "minimax/minimax-m3") == "minimax/minimax-m3"
        )

    def test_empty_string_is_explicitly_unpriced(self):
        assert _pricing_id_for("gemma-4-moe", "") is None
        assert _pricing_id_for("gemma-4-moe", "   ") is None

    def test_none_falls_back_to_normalized_id(self):
        assert _pricing_id_for("GPT-5.5", None) == "gpt-5.5"


# --- fetch_openrouter_prices ------------------------------------------------


class TestFetch:
    @pytest.mark.asyncio
    async def test_parses_and_skips_unpriced(self):
        def handler(_req):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "openai/gpt-5.5",
                            "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
                        },
                        {
                            "id": "free/model",
                            "pricing": {"prompt": "0", "completion": "0"},
                        },
                        {  # variable/BYO pricing → skipped
                            "id": "variable/model",
                            "pricing": {"prompt": "-1", "completion": "-1"},
                        },
                        {"id": "nopricing"},  # no pricing block → skipped
                    ]
                },
            )

        prices = await fetch_openrouter_prices(client=_client(handler))
        assert prices["openai/gpt-5.5"] == (Decimal("0.0000025"), Decimal("0.00001"))
        assert prices["free/model"] == (Decimal("0"), Decimal("0"))
        assert "variable/model" not in prices
        assert "nopricing" not in prices

    @pytest.mark.asyncio
    async def test_non_fatal_on_http_error(self):
        def handler(_req):
            return httpx.Response(500, json={})

        assert await fetch_openrouter_prices(client=_client(handler)) == {}


# --- sync_llm_rates ---------------------------------------------------------


class TestSyncLlmRates:
    _PRICES = {"openai/gpt-5.5": (Decimal("0.0000025"), Decimal("0.00001"))}
    _TS = datetime(2026, 7, 1, tzinfo=timezone.utc)

    @pytest.mark.asyncio
    async def test_inserts_both_token_dimensions(self):
        conn = FakeConn()
        n = await sync_llm_rates(
            FakePool(conn),
            [("gpt-5.5", "openai/gpt-5.5")],
            prices=self._PRICES,
            now=self._TS,
        )
        assert n == 2
        assert ("gpt-5.5", "prompt-token", Decimal("0.0000025")) in conn.inserts
        assert ("gpt-5.5", "completion-token", Decimal("0.00001")) in conn.inserts

    @pytest.mark.asyncio
    async def test_change_only_second_run_is_noop(self):
        conn = FakeConn()
        models = [("gpt-5.5", "openai/gpt-5.5")]
        await sync_llm_rates(FakePool(conn), models, prices=self._PRICES, now=self._TS)
        # Same prices again → no new rows.
        n2 = await sync_llm_rates(
            FakePool(conn), models, prices=self._PRICES, now=self._TS
        )
        assert n2 == 0

    @pytest.mark.asyncio
    async def test_price_change_inserts_new_row(self):
        conn = FakeConn()
        models = [("gpt-5.5", "openai/gpt-5.5")]
        await sync_llm_rates(FakePool(conn), models, prices=self._PRICES, now=self._TS)
        bumped = {"openai/gpt-5.5": (Decimal("0.0000030"), Decimal("0.00001"))}
        n2 = await sync_llm_rates(FakePool(conn), models, prices=bumped, now=self._TS)
        assert n2 == 1  # only the prompt-token rate changed
        assert ("gpt-5.5", "prompt-token", Decimal("0.0000030")) in conn.inserts

    @pytest.mark.asyncio
    async def test_explicitly_unpriced_model_is_skipped(self):
        conn = FakeConn()
        n = await sync_llm_rates(
            FakePool(conn),
            [("gemma-4-moe", "")],  # self-hosted → unpriced
            prices=self._PRICES,
            now=self._TS,
        )
        assert n == 0
        assert conn.inserts == []

    @pytest.mark.asyncio
    async def test_unmatched_pricing_id_left_unpriced(self):
        conn = FakeConn()
        n = await sync_llm_rates(
            FakePool(conn),
            [("mystery", "openrouter/not-in-catalog")],
            prices=self._PRICES,
            now=self._TS,
        )
        assert n == 0

    @pytest.mark.asyncio
    async def test_no_pool_is_noop(self):
        assert await sync_llm_rates(None, [("gpt-5.5", "openai/gpt-5.5")]) == 0
