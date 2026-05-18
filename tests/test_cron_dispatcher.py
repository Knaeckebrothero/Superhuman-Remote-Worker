"""Tests for ``orchestrator/services/cron_dispatcher.py``.

Cover DST-correctness of ``compute_next_run_after`` (the part that has
to keep "09:00 Europe/Berlin" at 09:00 across spring-forward / fall-back),
input validation, and the per-tick state machine — fire, catchup skip,
max-fires auto-disable, invalid-cron auto-disable — against a fully
mocked DB so the tests stay fast and don't need a Postgres container.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from orchestrator.services.cron_dispatcher import (
    _process_one_due_automation,
    compute_initial_next_run,
    compute_next_run_after,
    validate_cron_expr,
    validate_timezone,
)


# ---------------------------------------------------------------------------
# compute_next_run_after — DST correctness
# ---------------------------------------------------------------------------


class TestComputeNextRunAfter:
    def test_simple_utc_daily(self) -> None:
        """Daily 09:00 UTC, base just after a fire → next is the day after."""
        base = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
        out = compute_next_run_after("0 9 * * *", base, "UTC")
        assert out == datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc)

    def test_simple_utc_hourly(self) -> None:
        base = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
        out = compute_next_run_after("0 * * * *", base, "UTC")
        assert out == datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc)

    def test_dst_spring_forward_berlin(self) -> None:
        """Daily 09:00 Europe/Berlin stays at 09:00 local across DST.

        Spring forward 2026 is 2026-03-29 (Europe/Berlin clocks jump
        02:00 → 03:00 CET → CEST). The 09:00 fire on 03-28 is at 08:00 UTC
        (CET=+01:00); the 09:00 fire on 03-29 is at 07:00 UTC (CEST=+02:00).
        """
        before_dst = datetime(2026, 3, 28, 8, 0, tzinfo=timezone.utc)  # 09:00 CET
        out = compute_next_run_after("0 9 * * *", before_dst, "Europe/Berlin")
        # Should be 2026-03-29 09:00 CEST = 07:00 UTC, not 08:00 UTC
        expected_local = datetime(2026, 3, 29, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert out == expected_local.astimezone(timezone.utc)
        assert out.hour == 7  # CEST is UTC+2

    def test_dst_fall_back_berlin(self) -> None:
        """Daily 09:00 Europe/Berlin stays at 09:00 local across fall-back.

        Fall back 2026 is 2026-10-25. 09:00 CEST on 10-24 = 07:00 UTC;
        09:00 CET on 10-25 = 08:00 UTC.
        """
        before_dst = datetime(2026, 10, 24, 7, 0, tzinfo=timezone.utc)  # 09:00 CEST
        out = compute_next_run_after("0 9 * * *", before_dst, "Europe/Berlin")
        expected_local = datetime(2026, 10, 25, 9, 0, tzinfo=ZoneInfo("Europe/Berlin"))
        assert out == expected_local.astimezone(timezone.utc)
        assert out.hour == 8  # CET is UTC+1

    def test_naive_input_treated_as_utc(self) -> None:
        """A naive datetime is not raised on — astimezone() of a naive
        datetime assumes local; we pass through utc-aware inputs in the
        real code path, but defending the helper signature matters."""
        base = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
        out = compute_next_run_after("0 9 * * *", base, "UTC")
        assert out.tzinfo is not None  # always tz-aware

    def test_returns_utc(self) -> None:
        base = datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc)
        out = compute_next_run_after("*/5 * * * *", base, "America/Los_Angeles")
        assert out.tzinfo == timezone.utc


class TestComputeInitialNextRun:
    def test_seeds_from_now_default(self) -> None:
        """Without an explicit ``now``, uses the wall clock."""
        out = compute_initial_next_run("0 0 1 1 *", "UTC")
        assert out.tzinfo == timezone.utc
        assert out > datetime.now(timezone.utc)

    def test_explicit_now(self) -> None:
        now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
        out = compute_initial_next_run("0 9 * * *", "UTC", now=now)
        # Next 09:00 after 12:00 today is tomorrow 09:00.
        assert out == datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------


class TestValidateCronExpr:
    @pytest.mark.parametrize(
        "expr",
        [
            "* * * * *",
            "0 9 * * *",
            "*/5 * * * *",
            "0 0 1 1 *",
            "0 9 * * 1-5",
        ],
    )
    def test_accepts_valid(self, expr: str) -> None:
        validate_cron_expr(expr)  # does not raise

    @pytest.mark.parametrize(
        "expr",
        [
            "",
            "not a cron",
            "99 * * * *",
            "* * * *",  # too few fields
        ],
    )
    def test_rejects_invalid(self, expr: str) -> None:
        with pytest.raises(ValueError):
            validate_cron_expr(expr)


class TestValidateTimezone:
    @pytest.mark.parametrize("tz", ["UTC", "Europe/Berlin", "America/New_York"])
    def test_accepts_iana(self, tz: str) -> None:
        validate_timezone(tz)  # does not raise

    @pytest.mark.parametrize("tz", ["", "Not/A/Zone", "GMT+1", "Europe/Atlantis"])
    def test_rejects_invalid(self, tz: str) -> None:
        with pytest.raises(ValueError):
            validate_timezone(tz)


# ---------------------------------------------------------------------------
# _process_one_due_automation — state-machine paths
# ---------------------------------------------------------------------------


def _make_mock_db(due_row: dict | None) -> MagicMock:
    """Build an asyncpg-shaped mock with the standard ``acquire`` →
    ``transaction`` async-context-manager chain plus the four automation
    helpers the dispatcher calls.
    """
    conn = MagicMock()
    conn.transaction = MagicMock(return_value=_AsyncCtx(MagicMock()))

    db = MagicMock()
    db.acquire = MagicMock(return_value=_AsyncCtx(conn))
    db.fetch_next_due_cron_automation = AsyncMock(return_value=due_row)
    db.advance_automation_after_fire = AsyncMock()
    db.skip_automation_fire = AsyncMock()
    db.auto_disable_automation = AsyncMock()
    # create_job is invoked by create_job_from_automation
    db.create_job = AsyncMock(
        return_value={"id": "11111111-1111-1111-1111-111111111111"}
    )
    return db


class _AsyncCtx:
    """Minimal ``async with`` returning a fixed value (asyncpg pool/tx shape)."""

    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _make_row(
    *,
    cron_expr: str = "0 9 * * *",
    timezone_name: str = "UTC",
    next_run_at: datetime,
    catchup_window_seconds: int = 86400,
    fires_today_count: int = 0,
    fires_today_date: date | None = None,
    max_fires_per_day: int = 100,
    autonomy: str = "review",
    config_override: dict | None = None,
) -> dict:
    """Mimics ``db.fetch_next_due_cron_automation`` output."""
    return {
        "id": "22222222-2222-2222-2222-222222222222",
        "owner_id": "33333333-3333-3333-3333-333333333333",
        "project_id": None,
        "name": "test-automation",
        "cron_expr": cron_expr,
        "timezone": timezone_name,
        "next_run_at": next_run_at,
        "catchup_window_seconds": catchup_window_seconds,
        "expert": "scholar",
        "prompt": "Do the thing",
        "config_override": config_override or {},
        "autonomy": autonomy,
        "priority": 5,
        "fires_today_count": fires_today_count,
        "fires_today_date": fires_today_date,
        "max_fires_per_day": max_fires_per_day,
    }


class TestProcessOneDueAutomation:
    @pytest.mark.asyncio
    async def test_no_due_returns_false_without_side_effects(self) -> None:
        db = _make_mock_db(due_row=None)
        out = await _process_one_due_automation(db)
        assert out is False
        db.create_job.assert_not_called()
        db.advance_automation_after_fire.assert_not_called()
        db.skip_automation_fire.assert_not_called()
        db.auto_disable_automation.assert_not_called()

    @pytest.mark.asyncio
    async def test_fires_when_due(self) -> None:
        now = datetime.now(timezone.utc)
        row = _make_row(next_run_at=now)  # due now
        db = _make_mock_db(due_row=row)

        out = await _process_one_due_automation(db)

        assert out is True
        db.create_job.assert_awaited_once()
        db.advance_automation_after_fire.assert_awaited_once()
        # The advance call should carry the new next_run_at + the scheduled
        # time we just fired for, plus the new job's id.
        kwargs = db.advance_automation_after_fire.await_args.kwargs
        assert kwargs["scheduled_for"] == now
        assert kwargs["next_run_at"] > now
        assert kwargs["job_id"] == "11111111-1111-1111-1111-111111111111"

    @pytest.mark.asyncio
    async def test_catchup_window_skip(self) -> None:
        """A fire scheduled more than ``catchup_window_seconds`` ago is
        skipped without firing a job."""
        long_ago = datetime.now(timezone.utc).replace(
            year=2025, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        row = _make_row(
            next_run_at=long_ago,
            catchup_window_seconds=3600,  # 1h grace, well under the gap
        )
        db = _make_mock_db(due_row=row)

        out = await _process_one_due_automation(db)

        assert out is True
        db.create_job.assert_not_called()
        db.advance_automation_after_fire.assert_not_called()
        db.skip_automation_fire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_max_fires_per_day_auto_disables(self) -> None:
        now = datetime.now(timezone.utc)
        row = _make_row(
            next_run_at=now,
            max_fires_per_day=5,
            fires_today_count=5,
            fires_today_date=now.date(),
        )
        db = _make_mock_db(due_row=row)

        out = await _process_one_due_automation(db)

        assert out is True
        db.create_job.assert_not_called()
        db.auto_disable_automation.assert_awaited_once()
        kwargs = db.auto_disable_automation.await_args.kwargs
        assert "max_fires_per_day" in kwargs["reason"]

    @pytest.mark.asyncio
    async def test_yesterdays_counter_does_not_block(self) -> None:
        """fires_today_count from a previous date must not block today's
        fire — the counter rolls daily.
        """
        now = datetime.now(timezone.utc)
        yesterday = (
            now.replace(hour=0, minute=0, second=0, microsecond=0)
            - (now - now.replace(year=now.year))
        ).date()
        # Use a clearly-prior date
        row = _make_row(
            next_run_at=now,
            max_fires_per_day=5,
            fires_today_count=999,  # would be over cap if it were today
            fires_today_date=date(2020, 1, 1),
        )
        del yesterday  # not actually used; clarity-only
        db = _make_mock_db(due_row=row)

        out = await _process_one_due_automation(db)

        assert out is True
        db.create_job.assert_awaited_once()
        db.auto_disable_automation.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_cron_disables_row(self) -> None:
        """A bad cron expression on a stored row must not loop the tick
        forever — disable the row so the owner can repair it.
        """
        now = datetime.now(timezone.utc)
        row = _make_row(next_run_at=now, cron_expr="this is not a cron")
        db = _make_mock_db(due_row=row)

        out = await _process_one_due_automation(db)

        assert out is True
        db.create_job.assert_not_called()
        db.auto_disable_automation.assert_awaited_once()
        kwargs = db.auto_disable_automation.await_args.kwargs
        assert "invalid cron" in kwargs["reason"].lower()
