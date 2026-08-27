"""Phase 6 (D-1) — the usage rollup.

Two halves, both pinned here:

  * :func:`_split_window` / :func:`_merge_sum` — the pure window-partition +
    fold arithmetic that keeps "rollup for closed days + raw for the tail"
    identical to a single-pass aggregation (unit tests, no DB).
  * :class:`UsageRollup` against a real Postgres (testcontainers): the cross-DB
    aggregate + watermark pass reconciles with a raw GROUP BY over a seeded
    window incl. a backdated month, is idempotent, catches late arrivals via the
    trailing re-close window, collapses NULL-dim rows into one bucket, and the
    serving methods return exactly what a pure-raw query would — including the
    partial low-boundary day of a mid-day window and the open "today" tail.

The auditdb ``usage_events`` ledger and the app-DB ``usage_daily`` / ``rollup_state``
live in one container here (they are separate servers in prod); the rollup reads
the first and writes the second, so a single pool standing in for both exercises
the same code path. Dates are FIXED (not wall-clock) so day math is deterministic.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytest.importorskip("testcontainers.postgres")

import asyncpg  # noqa: E402
import pytest_asyncio  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from orchestrator.services.usage_ledger import UsageLedger, UsageRates  # noqa: E402
from orchestrator.services.usage_rollup import (  # noqa: E402
    UsageRollup,
    _merge_sum,
    _split_window,
)

UTC = timezone.utc
# Fixed "now" for run_pass: newest closeable day = (NOW - 15min).date() - 1 = 03-14.
NOW = datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC)
W = date(2026, 3, 14)  # the resulting watermark

UA = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
UB = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
PP = uuid.UUID("cccccccc-0000-0000-0000-000000000003")


def _mid(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _split_window — the day/timestamp boundary arithmetic (pure).
# ---------------------------------------------------------------------------


class TestSplitWindow:
    def test_watermark_none_is_all_raw(self):
        rollup, raw = _split_window(_mid(date(2026, 1, 1)), NOW, None)
        assert rollup is None
        assert raw == [(_mid(date(2026, 1, 1)), NOW)]

    def test_empty_window(self):
        assert _split_window(NOW, NOW, W) == (None, [])
        assert _split_window(NOW, NOW - timedelta(hours=1), W) == (None, [])

    def test_midday_from_yields_low_and_high_raw_partials(self):
        frm = datetime(2026, 1, 20, 14, tzinfo=UTC)
        rollup, raw = _split_window(frm, NOW, W)
        # Whole days 01-21..03-14 from the rollup; the 01-20 tail + today raw.
        assert rollup == (date(2026, 1, 21), date(2026, 3, 14))
        assert raw == [
            (frm, _mid(date(2026, 1, 21))),
            (_mid(date(2026, 3, 15)), NOW),
        ]

    def test_midnight_aligned_window_is_pure_rollup(self):
        rollup, raw = _split_window(_mid(date(2026, 1, 20)), _mid(date(2026, 3, 15)), W)
        assert rollup == (date(2026, 1, 20), date(2026, 3, 14))
        assert raw == []  # both boundaries land on midnights → no partials

    def test_window_entirely_in_open_tail(self):
        # from today's midnight to mid-today: no whole closed day fits.
        frm = _mid(date(2026, 3, 15))
        rollup, raw = _split_window(frm, NOW, W)
        assert rollup is None
        assert raw == [(frm, NOW)]

    def test_window_entirely_in_closed_days(self):
        rollup, raw = _split_window(_mid(date(2026, 1, 20)), _mid(date(2026, 2, 1)), W)
        assert rollup == (date(2026, 1, 20), date(2026, 1, 31))
        assert raw == []

    def test_clamps_to_earlier_watermark(self):
        rollup, raw = _split_window(_mid(date(2026, 1, 20)), NOW, date(2026, 2, 15))
        assert rollup == (date(2026, 1, 20), date(2026, 2, 15))
        # Days after the watermark (02-16 .. today) fall to raw.
        assert raw == [(_mid(date(2026, 2, 16)), NOW)]


class TestMergeSum:
    def test_sums_numeric_measures_across_parts(self):
        a = [
            {"category": "llm", "unit": "prompt-token", "quantity": 100.0, "events": 2}
        ]
        b = [
            {"category": "llm", "unit": "prompt-token", "quantity": 30.0, "events": 1},
            {"category": "compute", "unit": "vcpu-hour", "quantity": 2.0, "events": 1},
        ]
        out = {
            (r["category"], r["unit"]): r
            for r in _merge_sum([a, b], ("category", "unit"))
        }
        assert out[("llm", "prompt-token")]["quantity"] == 130.0
        assert out[("llm", "prompt-token")]["events"] == 3
        assert out[("compute", "vcpu-hour")]["quantity"] == 2.0

    def test_booleans_are_not_summed(self):
        rows = [{"key": "u1", "unit": "x", "events": 1, "is_admin": True}]
        merged = _merge_sum([rows, rows], ("key", "unit"))
        assert len(merged) == 1
        assert merged[0]["events"] == 2
        assert merged[0]["is_admin"] is True  # first-value, not summed


# ---------------------------------------------------------------------------
# UsageRollup against a real Postgres.
# ---------------------------------------------------------------------------

# (ts, user_id, project_id, category, resource, quantity, unit, cost_usd, source_id)
_SEED = [
    (
        datetime(2026, 1, 20, 10, tzinfo=UTC),
        UA,
        PP,
        "llm",
        "model-x",
        100,
        "prompt-token",
        "0.010",
        "s1",
    ),
    (
        datetime(2026, 1, 20, 10, tzinfo=UTC),
        UA,
        PP,
        "llm",
        "model-x",
        50,
        "cached-prompt-token",
        "0.001",
        "s1-cache",
    ),
    (
        datetime(2026, 1, 20, 15, tzinfo=UTC),
        UA,
        PP,
        "llm",
        "model-x",
        40,
        "completion-token",
        "0.004",
        "s2",
    ),
    (
        datetime(2026, 2, 10, 10, tzinfo=UTC),
        UA,
        PP,
        "llm",
        "model-x",
        50,
        "completion-token",
        "0.005",
        "s3",
    ),
    # unattributed (user_id / project_id NULL) — the NULLS NOT DISTINCT bucket.
    (
        datetime(2026, 2, 10, 12, tzinfo=UTC),
        None,
        None,
        "llm",
        "model-z",
        30,
        "prompt-token",
        "0.003",
        "s4",
    ),
    # Existing pre-v2 point-event categories must survive the compatibility
    # filter when typed infrastructure classes begin sharing the ledger.
    (
        datetime(2026, 2, 10, 13, tzinfo=UTC),
        UA,
        PP,
        "tts",
        "voice-a",
        120,
        "tts-character",
        "0.002",
        "speech-tts",
    ),
    (
        datetime(2026, 2, 10, 14, tzinfo=UTC),
        UA,
        PP,
        "stt",
        "transcriber-a",
        1,
        "stt-request",
        "0.003",
        "speech-stt",
    ),
    (
        datetime(2026, 3, 14, 10, tzinfo=UTC),
        UA,
        PP,
        "llm",
        "model-y",
        200,
        "prompt-token",
        "0.020",
        "s5",
    ),
    (
        datetime(2026, 3, 14, 11, tzinfo=UTC),
        UB,
        None,
        "compute",
        "workspace_pod",
        2,
        "vcpu-hour",
        None,
        "s6",
    ),
    # today — after the watermark; must be served raw, never rolled up.
    (
        datetime(2026, 3, 15, 9, tzinfo=UTC),
        UA,
        PP,
        "llm",
        "model-y",
        999,
        "prompt-token",
        "0.500",
        "s7",
    ),
]

_PARTS = [
    ("2025_12", "2025-12-01", "2026-01-01"),
    ("2026_01", "2026-01-01", "2026-02-01"),
    ("2026_02", "2026-02-01", "2026-03-01"),
    ("2026_03", "2026-03-01", "2026-04-01"),
    ("2026_04", "2026-04-01", "2026-05-01"),
]


async def _setup_schema(pool: asyncpg.Pool) -> None:
    """Build a faithful (minimal) usage_events + usage_daily + rollup_state."""
    async with pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS usage_events CASCADE")
        await conn.execute("DROP TABLE IF EXISTS usage_daily")
        await conn.execute("DROP TABLE IF EXISTS rollup_state")
        await conn.execute(
            """
            CREATE TABLE usage_events (
                id BIGSERIAL, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                user_id UUID, project_id UUID, ref_kind TEXT, ref_id UUID,
                category TEXT NOT NULL, resource TEXT NOT NULL,
                quantity NUMERIC NOT NULL, unit TEXT NOT NULL,
                rate_usd NUMERIC, cost_usd NUMERIC,
                source TEXT NOT NULL, source_id TEXT NOT NULL,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                PRIMARY KEY (id, ts)
            ) PARTITION BY RANGE (ts)
            """
        )
        for suffix, lo, hi in _PARTS:
            await conn.execute(
                f"CREATE TABLE usage_events_p{suffix} PARTITION OF usage_events "
                f"FOR VALUES FROM ('{lo}') TO ('{hi}')"
            )
        # Mirror migrations/app/0047 exactly (the NULLS NOT DISTINCT index is the
        # load-bearing bit for the unattributed bucket's upsert).
        await conn.execute(
            """
            CREATE TABLE usage_daily (
                day DATE NOT NULL, user_id UUID, project_id UUID,
                category TEXT NOT NULL, resource TEXT NOT NULL, unit TEXT NOT NULL,
                quantity NUMERIC NOT NULL, cost_usd NUMERIC NOT NULL DEFAULT 0,
                events BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            "CREATE UNIQUE INDEX usage_daily_dims_idx ON usage_daily "
            "(day, user_id, project_id, category, resource, unit) NULLS NOT DISTINCT"
        )
        await conn.execute(
            "CREATE TABLE rollup_state (name TEXT PRIMARY KEY, last_closed_day DATE, "
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        await conn.execute(
            "INSERT INTO rollup_state (name, last_closed_day) VALUES ('usage_daily', NULL)"
        )


async def _seed(pool: asyncpg.Pool, rows=_SEED) -> None:
    async with pool.acquire() as conn:
        for ts, uid, pid, cat, res, qty, unit, cost, sid in rows:
            await conn.execute(
                "INSERT INTO usage_events "
                "(ts, user_id, project_id, category, resource, quantity, unit, "
                " cost_usd, source, source_id) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'test',$9)",
                ts,
                uid,
                pid,
                cat,
                res,
                Decimal(str(qty)),
                unit,
                Decimal(cost) if cost is not None else None,
                sid,
            )


async def _raw_agg(pool: asyncpg.Pool, lo: datetime, hi: datetime) -> dict:
    """The reference aggregation the rollup must reproduce, keyed by full dims."""
    rows = await pool.fetch(
        """
        SELECT (ts AT TIME ZONE 'UTC')::date AS day, user_id, project_id,
               category, resource, unit,
               SUM(quantity) AS q, COALESCE(SUM(cost_usd),0) AS c, COUNT(*) AS e
        FROM usage_events WHERE ts >= $1 AND ts < $2
        GROUP BY 1,2,3,4,5,6
        """,
        lo,
        hi,
    )
    return {
        (
            r["day"],
            r["user_id"],
            r["project_id"],
            r["category"],
            r["resource"],
            r["unit"],
        ): (r["q"], r["c"], r["e"])
        for r in rows
    }


async def _rollup_rows(pool: asyncpg.Pool) -> dict:
    rows = await pool.fetch("SELECT * FROM usage_daily")
    return {
        (
            r["day"],
            r["user_id"],
            r["project_id"],
            r["category"],
            r["resource"],
            r["unit"],
        ): (r["quantity"], r["cost_usd"], r["events"])
        for r in rows
    }


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    pool = await asyncpg.create_pool(pg_dsn)
    await _setup_schema(pool)
    yield pool
    await pool.close()


def _rollup(pool) -> UsageRollup:
    return UsageRollup(pool, pool, UsageLedger(pool, UsageRates(pool)))


def _norm(by_category) -> list:
    return sorted(
        (
            r["category"],
            r["unit"],
            round(r["quantity"], 6),
            round(r["cost_usd"], 6),
            r["events"],
        )
        for r in by_category
    )


class TestRunPass:
    pytestmark = pytest.mark.asyncio

    async def test_reconciles_with_raw_over_closed_window(self, db):
        await _seed(db)
        res = await _rollup(db).run_pass(now=NOW)
        assert res["last_closed_day"] == W
        # usage_daily == a raw GROUP BY over [earliest .. midnight(watermark+1)).
        expected = await _raw_agg(db, _mid(date(2026, 1, 20)), _mid(date(2026, 3, 15)))
        assert await _rollup_rows(db) == expected

    async def test_excludes_open_today(self, db):
        await _seed(db)
        await _rollup(db).run_pass(now=NOW)
        days = {r["day"] for r in await db.fetch("SELECT day FROM usage_daily")}
        assert date(2026, 3, 15) not in days  # today is served raw, never rolled up
        assert date(2026, 3, 14) in days

    async def test_advances_watermark(self, db):
        await _seed(db)
        await _rollup(db).run_pass(now=NOW)
        wm = await db.fetchval(
            "SELECT last_closed_day FROM rollup_state WHERE name='usage_daily'"
        )
        assert wm == W

    async def test_idempotent(self, db):
        await _seed(db)
        r = _rollup(db)
        await r.run_pass(now=NOW)
        first = await _rollup_rows(db)
        await r.run_pass(now=NOW)  # re-run: trailing re-close, same totals
        assert await _rollup_rows(db) == first

    async def test_null_dims_collapse_to_one_bucket(self, db):
        await _seed(db)
        r = _rollup(db)
        await r.run_pass(now=NOW)
        await r.run_pass(now=NOW)  # a second pass must UPDATE, not duplicate
        n = await db.fetchval(
            "SELECT count(*) FROM usage_daily WHERE user_id IS NULL AND project_id IS NULL"
        )
        assert n == 1  # the s4 unattributed row, upserted in place both passes

    async def test_trailing_reclose_catches_recent_late_arrival(self, db):
        await _seed(db)
        r = _rollup(db)
        await r.run_pass(now=NOW)
        before = (await _rollup_rows(db))[
            (date(2026, 3, 14), UA, PP, "llm", "model-y", "prompt-token")
        ]
        # A late row lands for 03-14 (within the trailing-7 window) after close.
        await _seed(
            db,
            [
                (
                    datetime(2026, 3, 14, 20, tzinfo=UTC),
                    UA,
                    PP,
                    "llm",
                    "model-y",
                    5,
                    "prompt-token",
                    "0.001",
                    "late1",
                )
            ],
        )
        await r.run_pass(now=NOW)
        after = (await _rollup_rows(db))[
            (date(2026, 3, 14), UA, PP, "llm", "model-y", "prompt-token")
        ]
        assert after[0] == before[0] + 5  # quantity re-closed
        assert after[2] == before[2] + 1  # event count re-closed

    async def test_wide_lookback_catches_old_late_arrival(self, db):
        await _seed(db)
        r = _rollup(db)
        await r.run_pass(now=NOW)  # watermark = 03-14
        # A very-late row for 01-20 (far outside the 7-day trailing window): the
        # normal pass must MISS it, a wide-lookback pass must catch it.
        await _seed(
            db,
            [
                (
                    datetime(2026, 1, 20, 20, tzinfo=UTC),
                    UA,
                    PP,
                    "llm",
                    "model-x",
                    7,
                    "prompt-token",
                    "0.001",
                    "late2",
                )
            ],
        )
        key = (date(2026, 1, 20), UA, PP, "llm", "model-x", "prompt-token")
        await r.run_pass(now=NOW)  # trailing 7 → does not reach 01-20
        assert (await _rollup_rows(db))[key][0] == 100  # unchanged
        await r.run_pass(now=NOW, trailing_days=90)  # wide catch-up reaches back
        assert (await _rollup_rows(db))[key][0] == 107

    async def test_empty_ledger_advances_watermark(self, db):
        # No usage at all → watermark still advances so the first real pass starts
        # from the trailing window, not an epoch scan.
        res = await _rollup(db).run_pass(now=NOW)
        assert res["last_closed_day"] == W
        assert await db.fetchval("SELECT count(*) FROM usage_daily") == 0


class TestServingEquivalence:
    """rollup(closed) + raw(tail) must equal a pure-raw query for the same window."""

    pytestmark = pytest.mark.asyncio

    async def test_usage_full_window_matches_raw(self, db):
        await _seed(db)
        r = _rollup(db)
        await r.run_pass(now=NOW)
        frm, to = _mid(date(2026, 1, 1)), NOW
        rolled = await r.usage(from_ts=frm, to_ts=to)
        raw = await UsageLedger(db, UsageRates(db)).query_usage(from_ts=frm, to_ts=to)
        assert _norm(rolled["by_category"]) == _norm(raw["by_category"])
        assert round(rolled["total_cost_usd"], 6) == round(raw["total_cost_usd"], 6)
        assert round(rolled["cache_hit_ratio"], 6) == round(raw["cache_hit_ratio"], 6)
        categories = {row["category"] for row in rolled["by_category"]}
        assert {"llm", "tts", "stt", "compute"} <= categories

    async def test_usage_midday_window_low_partial_matches_raw(self, db):
        # from 01-20 14:00 excludes s1 (10:00) but includes s2 (15:00): the low
        # partial day must be served RAW, not over-counted from the daily rollup.
        await _seed(db)
        r = _rollup(db)
        await r.run_pass(now=NOW)
        frm, to = datetime(2026, 1, 20, 14, tzinfo=UTC), NOW
        rolled = await r.usage(from_ts=frm, to_ts=to)
        raw = await UsageLedger(db, UsageRates(db)).query_usage(from_ts=frm, to_ts=to)
        assert _norm(rolled["by_category"]) == _norm(raw["by_category"])
        # Sanity: s1's 100 prompt-token on 01-20 is excluded by the 14:00 floor.
        pt = next(
            x
            for x in rolled["by_category"]
            if x["category"] == "llm" and x["unit"] == "prompt-token"
        )
        assert pt["quantity"] == 200 + 30 + 999  # s5 + s4 + s7, NOT s1

    async def test_breakdown_matches_raw(self, db):
        await _seed(db)
        r = _rollup(db)
        await r.run_pass(now=NOW)
        frm, to = _mid(date(2026, 1, 1)), NOW
        for dim in ("user", "model", "project"):
            rolled = await r.breakdown(from_ts=frm, to_ts=to, group_by=dim)
            raw = await UsageLedger(db, UsageRates(db)).query_grouped(
                from_ts=frm, to_ts=to, group_by=dim
            )
            keyed_r = {(x["key"], x["unit"]): x for x in rolled}
            keyed_raw = {(x["key"], x["unit"]): x for x in raw}
            assert set(keyed_r) == set(keyed_raw)
            for k in keyed_raw:
                assert round(keyed_r[k]["quantity"], 6) == round(
                    keyed_raw[k]["quantity"], 6
                )
                assert keyed_r[k]["events"] == keyed_raw[k]["events"]

    async def test_timeseries_matches_raw(self, db):
        await _seed(db)
        r = _rollup(db)
        await r.run_pass(now=NOW)
        frm, to = _mid(date(2026, 1, 1)), NOW
        rolled = await r.timeseries(from_ts=frm, to_ts=to, group_by="model")
        raw = await UsageLedger(db, UsageRates(db)).query_timeseries(
            from_ts=frm, to_ts=to, group_by="model"
        )
        norm_r = sorted(
            (x["day"], x["key"], round(x["tokens"], 6), x["events"]) for x in rolled
        )
        norm_raw = sorted(
            (x["day"], x["key"], round(x["tokens"], 6), x["events"]) for x in raw
        )
        assert norm_r == norm_raw

    async def test_usage_self_scope_matches_raw(self, db):
        await _seed(db)
        r = _rollup(db)
        await r.run_pass(now=NOW)
        frm, to = _mid(date(2026, 1, 1)), NOW
        rolled = await r.usage(from_ts=frm, to_ts=to, owner_user_id=str(UA))
        raw = await UsageLedger(db, UsageRates(db)).query_usage(
            from_ts=frm, to_ts=to, owner_user_id=str(UA)
        )
        assert _norm(rolled["by_category"]) == _norm(raw["by_category"])
        # UB's compute + the unattributed row are not UA's → excluded.
        assert all(x["category"] != "compute" for x in rolled["by_category"])

    async def test_ref_id_bypasses_rollup_to_raw(self, db):
        # ref_id is not a rollup dim → the query must go straight to the raw ledger
        # (which is the only place per-job/thread cost lives).
        await _seed(
            db,
            [
                (
                    datetime(2026, 3, 14, 10, tzinfo=UTC),
                    UA,
                    PP,
                    "llm",
                    "model-y",
                    200,
                    "prompt-token",
                    "0.020",
                    "refseed",
                )
            ],
        )
        # tag it with a ref_id via a direct write path row:
        rid = uuid.uuid4()
        await db.execute(
            "UPDATE usage_events SET ref_kind='job', ref_id=$1 WHERE source_id='refseed'",
            rid,
        )
        r = _rollup(db)
        await r.run_pass(now=NOW)
        res = await r.usage(from_ts=_mid(date(2026, 1, 1)), to_ts=NOW, ref_id=str(rid))
        # Only the one ref-tagged row's usage comes back (proves raw, not rollup).
        assert len(res["by_category"]) == 1
        assert res["by_category"][0]["quantity"] == 200
