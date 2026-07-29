"""Officer cost + notify guards — daily token ceiling and quiet-hours pages.

The ceiling (centurion.md §4, third loop-guard layer) brakes the DRAIN: over
budget, claimed wake rows are deferred (not released — no attempts burned) to
the UTC reset and one day-stamped digest notice is written. Direct Legatus
input bypasses the drain, so a ceilinged officer still answers immediately.
Pages are the one urgency that crosses quiet hours (§6 notify contract).
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import session_wake

THREAD_ID = str(uuid.uuid4())


def _thread(ceiling=None, officer_state=None):
    officer = {"enabled": True}
    if ceiling is not None:
        officer["daily_token_ceiling"] = ceiling
    metadata = {"config_override": {"officer": officer}}
    if officer_state is not None:
        metadata["officer_state"] = officer_state
    return {"id": THREAD_ID, "metadata": metadata}


def _ledger(tokens, unit="tokens", available=True, raises=False):
    ledger = SimpleNamespace()
    ledger.is_available = available
    if raises:
        ledger.query_usage = AsyncMock(side_effect=RuntimeError("metering down"))
    else:
        ledger.query_usage = AsyncMock(
            return_value={
                "by_category": [{"unit": unit, "quantity": tokens}],
                "total_cost_usd": 0.0,
            }
        )
    return ledger


class TestCeilingParse:
    def test_absent_is_disabled(self):
        assert session_wake._officer_daily_ceiling(_thread()) == 0

    def test_parses_int(self):
        assert session_wake._officer_daily_ceiling(_thread(ceiling=500_000)) == 500_000

    def test_garbage_is_disabled(self):
        assert session_wake._officer_daily_ceiling(_thread(ceiling="lots")) == 0


class TestCeilingDeferral:
    @pytest.mark.asyncio
    async def test_disabled_ceiling_never_queries(self):
        ledger = _ledger(10**9)
        out = await session_wake._officer_ceiling_deferral(
            None, _thread(), usage_ledger=ledger
        )
        assert out is None
        ledger.query_usage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_under_ceiling_delivers(self):
        out = await session_wake._officer_ceiling_deferral(
            None, _thread(ceiling=100_000), usage_ledger=_ledger(99_999)
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_over_ceiling_defers_to_utc_reset(self):
        out = await session_wake._officer_ceiling_deferral(
            None, _thread(ceiling=100_000), usage_ledger=_ledger(100_000)
        )
        now = datetime.now(timezone.utc)
        assert out is not None
        assert out == (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    @pytest.mark.asyncio
    async def test_scoped_to_this_thread_today(self):
        ledger = _ledger(1)
        await session_wake._officer_ceiling_deferral(
            None, _thread(ceiling=100), usage_ledger=ledger
        )
        kwargs = ledger.query_usage.await_args.kwargs
        assert kwargs["ref_id"] == THREAD_ID
        assert kwargs["from_ts"].hour == 0 and kwargs["from_ts"].minute == 0

    @pytest.mark.asyncio
    async def test_non_token_units_do_not_count(self):
        out = await session_wake._officer_ceiling_deferral(
            None,
            _thread(ceiling=100),
            usage_ledger=_ledger(10**9, unit="seconds"),
        )
        assert out is None

    @pytest.mark.asyncio
    async def test_fails_open_when_metering_down(self):
        assert (
            await session_wake._officer_ceiling_deferral(
                None, _thread(ceiling=1), usage_ledger=_ledger(0, raises=True)
            )
            is None
        )
        assert (
            await session_wake._officer_ceiling_deferral(
                None, _thread(ceiling=1), usage_ledger=_ledger(0, available=False)
            )
            is None
        )


class TestCeilingNotice:
    @pytest.mark.asyncio
    async def test_writes_one_digest_entry(self):
        db = SimpleNamespace(merge_thread_officer_state=AsyncMock())
        deferred_to = datetime.now(timezone.utc) + timedelta(hours=6)
        await session_wake._note_ceiling_breach(db, THREAD_ID, _thread(), deferred_to)
        db.merge_thread_officer_state.assert_awaited_once()
        patch = db.merge_thread_officer_state.await_args.args[1]
        assert patch["ceiling_notice"] == datetime.now(timezone.utc).date().isoformat()
        assert patch["digest"][-1]["subject"] == "Daily token ceiling reached"

    @pytest.mark.asyncio
    async def test_idempotent_per_day(self):
        db = SimpleNamespace(merge_thread_officer_state=AsyncMock())
        today = datetime.now(timezone.utc).date().isoformat()
        thread = _thread(officer_state={"ceiling_notice": today})
        await session_wake._note_ceiling_breach(
            db, THREAD_ID, thread, datetime.now(timezone.utc)
        )
        db.merge_thread_officer_state.assert_not_awaited()


class TestDrainBrake:
    @pytest.mark.asyncio
    async def test_over_ceiling_defers_claimed_rows(self, monkeypatch):
        rows = [
            {"id": 1, "thread_id": THREAD_ID, "source": "timer", "dedup_key": "timer"},
            {"id": 2, "thread_id": THREAD_ID, "source": "loop", "dedup_key": "x:1"},
        ]
        db = SimpleNamespace()
        db.claim_pending_session_wake_events = AsyncMock(return_value=rows)
        db.get_thread = AsyncMock(return_value=_thread(ceiling=1))
        db.defer_session_wake_events = AsyncMock()
        db.finish_session_wake_events = AsyncMock()
        db.release_session_wake_events = AsyncMock()
        db.merge_thread_officer_state = AsyncMock()

        deferred_to = datetime.now(timezone.utc) + timedelta(hours=3)
        monkeypatch.setattr(
            session_wake,
            "_officer_ceiling_deferral",
            AsyncMock(return_value=deferred_to),
        )
        resolve = AsyncMock()
        monkeypatch.setattr(session_wake, "_resolve_live_agent", resolve)

        delivered = await session_wake.drain_pending_event_wakes(db)
        assert delivered == 0
        db.defer_session_wake_events.assert_awaited_once_with(
            [1, 2], fire_at=deferred_to
        )
        db.finish_session_wake_events.assert_not_awaited()
        db.release_session_wake_events.assert_not_awaited()
        resolve.assert_not_awaited()  # never even probed the pod
        db.merge_thread_officer_state.assert_awaited_once()  # the digest notice


class TestDeferPrimitive:
    @pytest.mark.asyncio
    async def test_defer_sets_fire_at_without_burning_attempts(self):
        from database.postgres import PostgresDB

        captured = {}

        async def execute(sql, *params):
            captured["sql"] = sql
            captured["params"] = params

        class _Acq:
            def __init__(self, conn):
                self._conn = conn

            async def __aenter__(self):
                return self._conn

            async def __aexit__(self, *a):
                return False

        conn = SimpleNamespace(execute=execute)
        db = PostgresDB.__new__(PostgresDB)
        db.acquire = lambda: _Acq(conn)
        fire_at = datetime.now(timezone.utc)
        await db.defer_session_wake_events([7, 9], fire_at=fire_at)
        sql = captured["sql"]
        assert "fire_at = $2" in sql
        assert "state = 'pending'" in sql
        assert "attempts" not in sql
        assert captured["params"] == ([7, 9], fire_at)


class TestQuietHoursPageBypass:
    def _service(self, in_quiet_hours=True):
        from services.notification_service import NotificationService

        svc = NotificationService.__new__(NotificationService)
        svc._available = True
        svc._get_user_channels = AsyncMock(return_value={"email": False})
        svc._get_user_settings = AsyncMock(return_value={})
        svc._is_in_quiet_hours = MagicMock(return_value=in_quiet_hours)
        svc._queue_notification = AsyncMock()
        svc._broadcast_sse = AsyncMock()
        svc._cockpit_url = "https://cockpit"
        svc._email_service = None
        svc._transports = {}
        return svc

    @pytest.mark.asyncio
    async def test_page_crosses_quiet_hours(self):
        svc = self._service(in_quiet_hours=True)
        results = await svc.dispatch(
            user_id="u",
            job_id=THREAD_ID,
            subject="s",
            message_md="m",
            job_description="officer page",
            config_name="centurion",
            bypass_quiet_hours=True,
        )
        assert results["queued"] is False
        svc._queue_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_still_queues_in_quiet_hours(self):
        svc = self._service(in_quiet_hours=True)
        results = await svc.dispatch(
            user_id="u",
            job_id=THREAD_ID,
            subject="s",
            message_md="m",
            job_description="digest",
            config_name="centurion",
        )
        assert results["queued"] is True
        svc._queue_notification.assert_awaited_once()
