"""S9 — conference embodiment + hold + brief wake (docs/features/centurion.md §2/§4).

A conference is an ordinary interactive session wearing the officer's
identity (officer.conference; enabled stays false). While one is open the
background officer is HELD: the wake-claim query skips held threads (rows —
timers included — stay pending), the watchdog stands down, and job dispatch
from the held officer thread is fenced. Conference end (deliberate or
idle-archive; the watchdog self-heals missed hooks) releases the hold and
enqueues the coalescing `conference` brief wake. Live delivery is k3d-smoke
territory, as with the substrate suite.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main

OFFICER_TID = str(uuid.uuid4())
CONF_TID = str(uuid.uuid4())
PROJECT_ID = str(uuid.uuid4())


def _officer_row(hold=None, officer_state=None, **over):
    officer = {"enabled": True, "max_pages_per_day": 3}
    if hold is not None:
        officer["hold"] = hold
    metadata = {"config_override": {"officer": officer}}
    if officer_state is not None:
        metadata["officer_state"] = officer_state
    row = {
        "id": OFFICER_TID,
        "project_id": PROJECT_ID,
        "status": "active",
        "title": "Centurion — Better Resavio",
        "created_at": None,
        "metadata": metadata,
    }
    row.update(over)
    return row


def _conference_row(status="active"):
    return {
        "id": CONF_TID,
        "project_id": PROJECT_ID,
        "status": status,
        "title": "Conference",
        "metadata": {"config_override": {"officer": {"conference": True}}},
    }


class TestConferencePredicate:
    def test_conference_thread_detected(self):
        assert main._thread_is_conference(_conference_row()) is True

    def test_officer_thread_is_not_a_conference(self):
        assert main._thread_is_conference(_officer_row()) is False

    def test_ordinary_thread_is_not_a_conference(self):
        assert main._thread_is_conference({"id": "x", "metadata": {}}) is False


class TestHoldStamp:
    @pytest.mark.asyncio
    async def test_stamps_hold_on_project_officer(self, monkeypatch):
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(return_value=_officer_row())
        db.merge_thread_config_override = AsyncMock(return_value=True)
        db.get_thread = AsyncMock(return_value=_officer_row())
        monkeypatch.setattr(main, "postgres_db", db)
        from services import session_wake as sw

        monkeypatch.setattr(sw, "_resolve_live_agent", AsyncMock(return_value=None))

        await main._hold_officer_for_conference(PROJECT_ID, CONF_TID)
        db.merge_thread_config_override.assert_awaited_once()
        tid, patch = db.merge_thread_config_override.await_args.args
        assert tid == OFFICER_TID
        hold = patch["officer"]["hold"]
        assert hold["kind"] == "conference"
        assert hold["thread_id"] == CONF_TID

    @pytest.mark.asyncio
    async def test_noop_without_officer(self, monkeypatch):
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(return_value=None)
        db.merge_thread_config_override = AsyncMock()
        monkeypatch.setattr(main, "postgres_db", db)
        await main._hold_officer_for_conference(PROJECT_ID, CONF_TID)
        db.merge_thread_config_override.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_holds_itself(self, monkeypatch):
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(
            return_value={"id": CONF_TID, "metadata": {}}
        )
        db.merge_thread_config_override = AsyncMock()
        monkeypatch.setattr(main, "postgres_db", db)
        await main._hold_officer_for_conference(PROJECT_ID, CONF_TID)
        db.merge_thread_config_override.assert_not_awaited()


class TestConferenceConclude:
    @pytest.mark.asyncio
    async def test_releases_hold_and_enqueues_brief(self, monkeypatch):
        held = _officer_row(hold={"kind": "conference", "thread_id": CONF_TID})
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(return_value=held)
        db.merge_thread_config_override = AsyncMock(return_value=True)
        db.enqueue_session_wake_event = AsyncMock(return_value=True)
        monkeypatch.setattr(main, "postgres_db", db)
        kick = MagicMock()
        monkeypatch.setattr(main, "_kick_officer_event_drain", kick)

        await main._conclude_conference_if_any(_conference_row(status="ended"))

        _, patch = db.merge_thread_config_override.await_args.args
        assert patch == {"officer": {"hold": None}}
        db.enqueue_session_wake_event.assert_awaited_once()
        args, kwargs = db.enqueue_session_wake_event.await_args
        assert args[0] == OFFICER_TID
        assert kwargs["source"] == "conference"
        assert kwargs["dedup_key"] == CONF_TID
        assert kwargs["payload"]["conference_thread_id"] == CONF_TID
        kick.assert_called_once()

    @pytest.mark.asyncio
    async def test_foreign_hold_left_standing(self, monkeypatch):
        other = str(uuid.uuid4())
        held = _officer_row(hold={"kind": "conference", "thread_id": other})
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(return_value=held)
        db.merge_thread_config_override = AsyncMock()
        db.enqueue_session_wake_event = AsyncMock(return_value=True)
        monkeypatch.setattr(main, "postgres_db", db)
        monkeypatch.setattr(main, "_kick_officer_event_drain", MagicMock())

        await main._conclude_conference_if_any(_conference_row(status="ended"))
        db.merge_thread_config_override.assert_not_awaited()
        db.enqueue_session_wake_event.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ordinary_session_noop(self, monkeypatch):
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock()
        monkeypatch.setattr(main, "postgres_db", db)
        await main._conclude_conference_if_any(
            {"id": CONF_TID, "project_id": PROJECT_ID, "metadata": {}}
        )
        db.get_officer_thread_for_project.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_raises(self, monkeypatch):
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(side_effect=RuntimeError("db"))
        monkeypatch.setattr(main, "postgres_db", db)
        await main._conclude_conference_if_any(_conference_row())  # no raise


class TestWatchdogHold:
    @pytest.mark.asyncio
    async def test_live_conference_stands_watchdog_down(self, monkeypatch):
        held = _officer_row(hold={"kind": "conference", "thread_id": CONF_TID})
        db = SimpleNamespace()
        db.get_thread = AsyncMock(return_value=_conference_row(status="active"))
        db.enqueue_session_wake_event = AsyncMock()
        monkeypatch.setattr(main, "postgres_db", db)
        conclude = AsyncMock()
        monkeypatch.setattr(main, "_conclude_conference_if_any", conclude)

        await main._officer_watchdog_check_one(held, SimpleNamespace())
        conclude.assert_not_awaited()
        db.enqueue_session_wake_event.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_hold_concluded(self, monkeypatch):
        held = _officer_row(hold={"kind": "conference", "thread_id": CONF_TID})
        ended_conf = _conference_row(status="ended")
        db = SimpleNamespace()
        db.get_thread = AsyncMock(return_value=ended_conf)
        monkeypatch.setattr(main, "postgres_db", db)
        conclude = AsyncMock()
        monkeypatch.setattr(main, "_conclude_conference_if_any", conclude)

        await main._officer_watchdog_check_one(held, SimpleNamespace())
        conclude.assert_awaited_once_with(ended_conf)

    @pytest.mark.asyncio
    async def test_suspended_conference_concludes_hold(self, monkeypatch):
        # Legatus walked away; attention sweep parked the conference. The
        # meeting is over — the officer must not be held all night.
        held = _officer_row(hold={"kind": "conference", "thread_id": CONF_TID})
        suspended = _conference_row(status="suspended")
        db = SimpleNamespace()
        db.get_thread = AsyncMock(return_value=suspended)
        monkeypatch.setattr(main, "postgres_db", db)
        conclude = AsyncMock()
        monkeypatch.setattr(main, "_conclude_conference_if_any", conclude)

        await main._officer_watchdog_check_one(held, SimpleNamespace())
        conclude.assert_awaited_once_with(suspended)

    @pytest.mark.asyncio
    async def test_vanished_conference_clears_hold(self, monkeypatch):
        held = _officer_row(hold={"kind": "conference", "thread_id": CONF_TID})
        db = SimpleNamespace()
        db.get_thread = AsyncMock(return_value=None)
        db.merge_thread_config_override = AsyncMock(return_value=True)
        monkeypatch.setattr(main, "postgres_db", db)

        await main._officer_watchdog_check_one(held, SimpleNamespace())
        _, patch = db.merge_thread_config_override.await_args.args
        assert patch == {"officer": {"hold": None}}


class TestOfficerSummaryEndpoint:
    @pytest.mark.asyncio
    async def test_summary_shape(self, monkeypatch):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date().isoformat()
        officer = _officer_row(
            officer_state={
                "pages": {"date": today, "count": 2},
                "digest": [{"at": "t", "subject": "s", "message": "m"}] * 12,
                "ceiling_notice": today,
            }
        )

        class _Acq:
            async def __aenter__(self):
                return SimpleNamespace(fetchval=AsyncMock(return_value=4))

            async def __aexit__(self, *a):
                return False

        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(return_value=officer)
        db.get_pending_officer_timer = AsyncMock(
            return_value={"fire_at": "2026-07-30T05:00:00Z"}
        )
        db.acquire = lambda: _Acq()
        monkeypatch.setattr(main, "postgres_db", db)
        monkeypatch.setattr(
            main, "require_approved_user", AsyncMock(return_value={"id": "u"})
        )
        monkeypatch.setattr(main, "require_project_member", AsyncMock())
        monkeypatch.setattr(
            main,
            "_find_open_conference_thread",
            AsyncMock(return_value=_conference_row()),
        )

        out = await main.get_project_officer_summary(MagicMock(), PROJECT_ID)
        assert out["officer"]["thread_id"] == OFFICER_TID
        assert out["next_wake_at"] == "2026-07-30T05:00:00Z"
        assert out["pending_events"] == 4
        assert out["pages_today"] == {"used": 2, "budget": 3}
        assert out["token_ceiling"]["deferred_today"] is True
        assert len(out["digest"]) == 10  # capped
        assert out["conference"]["thread_id"] == CONF_TID

    @pytest.mark.asyncio
    async def test_no_officer_renders_enable_prompt(self, monkeypatch):
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(return_value=None)
        monkeypatch.setattr(main, "postgres_db", db)
        monkeypatch.setattr(
            main, "require_approved_user", AsyncMock(return_value={"id": "u"})
        )
        monkeypatch.setattr(main, "require_project_member", AsyncMock())
        monkeypatch.setattr(
            main, "_find_open_conference_thread", AsyncMock(return_value=None)
        )
        out = await main.get_project_officer_summary(MagicMock(), PROJECT_ID)
        assert out == {"officer": None, "conference": None}
