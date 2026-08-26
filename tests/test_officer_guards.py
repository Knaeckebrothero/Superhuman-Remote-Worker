"""Officer cost + notify guards — daily token ceiling and quiet-hours pages.

The ceiling (centurion.md §4, third loop-guard layer) brakes the DRAIN: over
budget, claimed wake rows are deferred (not released — no attempts burned) to
the UTC reset and one day-stamped digest notice is written. Direct Legate
input bypasses the drain, so a ceilinged officer still answers immediately.
Pages are the one urgency that crosses quiet hours (§6 notify contract).
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import session_wake
from services.usage_ledger import llm_tokens_from_rows

THREAD_ID = str(uuid.uuid4())


def _thread(ceiling=None, officer_state=None):
    officer = {"enabled": True}
    if ceiling is not None:
        officer["daily_token_ceiling"] = ceiling
    metadata = {"config_override": {"officer": officer}}
    if officer_state is not None:
        metadata["officer_state"] = officer_state
    return {"id": THREAD_ID, "metadata": metadata}


def _ledger(tokens, unit=None, available=True, raises=False):
    """`unit=None` splits `tokens` across the three real LLM token units."""
    ledger = SimpleNamespace()
    ledger.is_available = available
    if raises:
        ledger.query_usage = AsyncMock(side_effect=RuntimeError("metering down"))
    else:
        if unit is not None:
            rows = [{"unit": unit, "quantity": tokens}]
        else:
            third = tokens // 3
            rows = [
                {"unit": "prompt-token", "quantity": tokens - 2 * third},
                {"unit": "cached-prompt-token", "quantity": third},
                {"unit": "completion-token", "quantity": third},
                {"unit": "vcpu-hour", "quantity": 999},
            ]
        ledger.query_usage = AsyncMock(
            return_value={"by_category": rows, "total_cost_usd": 0.0}
        )
    return ledger


class TestLlmTokensFromRows:
    def test_realistic_payload_sums_all_three_units(self):
        rows = [
            {"category": "llm", "unit": "prompt-token", "quantity": 1_200_000.0},
            {"category": "llm", "unit": "cached-prompt-token", "quantity": 250_000.0},
            {"category": "llm", "unit": "completion-token", "quantity": 50_000.0},
            {"category": "compute", "unit": "vcpu-hour", "quantity": 3.5},
        ]
        assert llm_tokens_from_rows(rows) == 1_500_000

    def test_defensive_about_missing_keys(self):
        rows = [
            {},
            {"unit": "prompt-token"},
            {"unit": "completion-token", "quantity": None},
        ]
        assert llm_tokens_from_rows(rows) == 0.0


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
    async def test_units_below_ceiling_individually_defer_when_summed(self):
        ledger = _ledger(0)
        ledger.query_usage = AsyncMock(
            return_value={
                "by_category": [
                    {"unit": "prompt-token", "quantity": 60_000},
                    {"unit": "cached-prompt-token", "quantity": 25_000},
                    {"unit": "completion-token", "quantity": 15_000},
                ],
                "total_cost_usd": 0.0,
            }
        )
        out = await session_wake._officer_ceiling_deferral(
            None, _thread(ceiling=100_000), usage_ledger=ledger
        )
        assert out is not None

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
    async def test_records_one_low_officer_runtime_row_and_stamps_the_day(
        self, monkeypatch
    ):
        from services import notification_service as ns

        record = AsyncMock()
        monkeypatch.setattr(ns.notification_service, "record", record)
        db = SimpleNamespace(merge_thread_officer_state=AsyncMock())
        deferred_to = datetime.now(timezone.utc) + timedelta(hours=6)
        thread = {**_thread(), "user_id": "legate-1"}
        await session_wake._note_ceiling_breach(db, THREAD_ID, thread, deferred_to)
        today = datetime.now(timezone.utc).date().isoformat()
        record.assert_awaited_once()
        kw = record.await_args.kwargs
        assert kw["recipient_id"] == "legate-1"
        assert (kw["category"], kw["severity"]) == ("officer_runtime", "low")
        assert kw["dedup_key"] == f"officer_ceiling:{THREAD_ID}:{today}"
        assert kw["subject"] == "Daily token ceiling reached"
        assert (kw["source_kind"], kw["source_id"]) == ("thread", THREAD_ID)
        db.merge_thread_officer_state.assert_awaited_once()
        patch = db.merge_thread_officer_state.await_args.args[1]
        assert patch == {"ceiling_notice": today}  # no digest ring any more

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
    @pytest.mark.parametrize(
        ("delivery_state", "expected_delivered"),
        [("queued", 0), ("admitted", 1)],
    )
    async def test_durable_officer_wake_settles_only_after_provider_admission(
        self,
        monkeypatch,
        delivery_state,
        expected_delivered,
    ):
        event_id = 91
        delivery_id = str(uuid.uuid4())
        rows = [
            {
                "id": event_id,
                "thread_id": THREAD_ID,
                "source": "timer",
                "dedup_key": "timer:durable",
            }
        ]
        assigned = [{**rows[0], "delivery_id": delivery_id}]
        db = SimpleNamespace(
            claim_pending_session_wake_events=AsyncMock(return_value=rows),
            assign_session_wake_delivery_groups=AsyncMock(return_value=assigned),
            get_session_wake_delivery_group=AsyncMock(return_value=assigned),
            get_thread=AsyncMock(return_value=_thread()),
            persist_thread_input_delivery=AsyncMock(
                return_value={
                    "delivery_id": delivery_id,
                    "state": delivery_state,
                }
            ),
            finish_session_wake_events=AsyncMock(),
            defer_session_wake_events_for_input=AsyncMock(),
            release_session_wake_events=AsyncMock(),
            merge_thread_officer_state=AsyncMock(),
        )
        monkeypatch.setattr(
            session_wake,
            "_resolve_live_agent",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            session_wake,
            "_officer_ceiling_deferral",
            AsyncMock(return_value=None),
        )
        from services import sitrep

        monkeypatch.setattr(
            sitrep,
            "build_wake_message",
            AsyncMock(return_value=("durable wake", {"fingerprint": "new"})),
        )

        assert await session_wake.drain_pending_event_wakes(db) == expected_delivered
        db.persist_thread_input_delivery.assert_awaited_once_with(
            thread_id=THREAD_ID,
            delivery_id=delivery_id,
            role="event",
            content="durable wake",
            source="officer_wake",
        )
        if delivery_state == "admitted":
            db.finish_session_wake_events.assert_awaited_once_with([event_id])
            db.defer_session_wake_events_for_input.assert_not_awaited()
            db.merge_thread_officer_state.assert_awaited_once_with(
                THREAD_ID, {"fingerprint": "new"}
            )
        else:
            db.finish_session_wake_events.assert_not_awaited()
            db.defer_session_wake_events_for_input.assert_awaited_once()
            db.merge_thread_officer_state.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runtime_incident_defers_before_sitrep_or_live_delivery(
        self, monkeypatch
    ):
        project_id = str(uuid.uuid4())
        rows = [
            {"id": 1, "thread_id": THREAD_ID, "source": "timer", "dedup_key": "timer"}
        ]
        db = SimpleNamespace()
        db.claim_pending_session_wake_events = AsyncMock(return_value=rows)
        delivery_id = str(uuid.uuid4())
        assigned = [{**row, "delivery_id": delivery_id} for row in rows]
        db.assign_session_wake_delivery_groups = AsyncMock(return_value=assigned)
        db.get_session_wake_delivery_group = AsyncMock(return_value=assigned)
        db.get_thread = AsyncMock(return_value={**_thread(), "project_id": project_id})
        db.defer_session_wake_events = AsyncMock()
        db.finish_session_wake_events = AsyncMock()
        db.release_session_wake_events = AsyncMock()
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        from services import runtime_actor

        gate = AsyncMock(return_value=(False, retry_at))
        monkeypatch.setattr(runtime_actor, "admit_officer_wake_for_runtime", gate)
        resolve = AsyncMock()
        monkeypatch.setattr(session_wake, "_resolve_live_agent", resolve)

        delivered = await session_wake.drain_pending_event_wakes(db)

        assert delivered == 0
        gate.assert_awaited_once_with(db, project_id=project_id, thread_id=THREAD_ID)
        db.defer_session_wake_events.assert_awaited_once_with([1], fire_at=retry_at)
        resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_over_ceiling_defers_claimed_rows(self, monkeypatch):
        rows = [
            {"id": 1, "thread_id": THREAD_ID, "source": "timer", "dedup_key": "timer"},
            {"id": 2, "thread_id": THREAD_ID, "source": "loop", "dedup_key": "x:1"},
        ]
        db = SimpleNamespace()
        db.claim_pending_session_wake_events = AsyncMock(return_value=rows)
        delivery_id = str(uuid.uuid4())
        assigned = [{**row, "delivery_id": delivery_id} for row in rows]
        db.assign_session_wake_delivery_groups = AsyncMock(return_value=assigned)
        db.get_session_wake_delivery_group = AsyncMock(return_value=assigned)
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


class TestPageVsDigestSeverity:
    """The notify contract's two tiers are two severities on the feed: a
    page is `high` (reaches the Legate now — the only urgency that crosses
    quiet hours by class rule is `critical`, so a page in quiet hours is
    DEFERRED to the window's end, never dropped), a digest is `low` (in-app,
    read at the next look). See tests/test_notification_record.py for the
    quiet-hours deferral itself."""

    def test_page_is_high_and_digest_is_low(self):
        from services import notification_catalog as cat

        spec = cat.category_spec("officer_question")
        assert spec.severity == "high"
        high = cat.steps_for(spec, "high")
        assert high and all(s.immediate for s in high)
        assert "email" in {s.channel for s in high}
        assert cat.steps_for(spec, "low") == ()
        assert cat.bypasses_quiet_hours(spec, "high") is False
