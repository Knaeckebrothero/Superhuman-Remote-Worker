"""S9 — conference embodiment + hold + brief wake (knowledge-base/knowledge/features/centurion.md §2/§4).

A conference is an ordinary interactive session wearing the officer's
identity (officer.conference; enabled stays false). While one is open the
background officer is HELD: the wake-claim query skips held threads (rows —
timers included — stay pending), the watchdog stands down, and job dispatch
from the held officer thread is fenced. Conference end (deliberate or
idle-archive; the watchdog self-heals missed hooks) releases the hold and
enqueues the coalescing `conference` brief wake. Live delivery is k3d-smoke
territory, as with the substrate suite.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import main

OFFICER_TID = str(uuid.uuid4())
CONF_TID = str(uuid.uuid4())
PROJECT_ID = str(uuid.uuid4())


def _officer_row(hold=None, officer_state=None, **over):
    officer = {"enabled": True}
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
        db.set_project_officer_hold = AsyncMock(
            return_value={"thread": _officer_row(), "routes": []}
        )
        db.get_thread = AsyncMock(return_value=_officer_row())
        monkeypatch.setattr(main, "postgres_db", db)
        from services import session_wake as sw

        monkeypatch.setattr(sw, "_resolve_live_agent", AsyncMock(return_value=None))

        await main._hold_officer_for_conference(PROJECT_ID, CONF_TID)
        db.set_project_officer_hold.assert_awaited_once()
        args, kwargs = db.set_project_officer_hold.await_args
        assert args == (PROJECT_ID,)
        assert kwargs["expected_thread_id"] == OFFICER_TID
        assert kwargs["route_reason"] == "officer_hold"
        hold = kwargs["hold"]
        assert hold["kind"] == "conference"
        assert hold["thread_id"] == CONF_TID

    @pytest.mark.asyncio
    async def test_noop_without_officer(self, monkeypatch):
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(return_value=None)
        db.set_project_officer_hold = AsyncMock()
        monkeypatch.setattr(main, "postgres_db", db)
        await main._hold_officer_for_conference(PROJECT_ID, CONF_TID)
        db.set_project_officer_hold.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_never_holds_itself(self, monkeypatch):
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(
            return_value={"id": CONF_TID, "metadata": {}}
        )
        db.set_project_officer_hold = AsyncMock()
        monkeypatch.setattr(main, "postgres_db", db)
        await main._hold_officer_for_conference(PROJECT_ID, CONF_TID)
        db.set_project_officer_hold.assert_not_awaited()


class TestConferenceConclude:
    @pytest.mark.asyncio
    async def test_releases_hold_and_enqueues_brief(self, monkeypatch):
        held = _officer_row(hold={"kind": "conference", "thread_id": CONF_TID})
        db = SimpleNamespace()
        db.get_officer_thread_for_project = AsyncMock(return_value=held)
        db.set_project_officer_hold = AsyncMock(
            return_value={"thread": _officer_row(), "routes": []}
        )
        db.enqueue_session_wake_event = AsyncMock(return_value=True)
        monkeypatch.setattr(main, "postgres_db", db)
        kick = MagicMock()
        monkeypatch.setattr(main, "_kick_officer_event_drain", kick)

        await main._conclude_conference_if_any(_conference_row(status="ended"))

        db.set_project_officer_hold.assert_awaited_once_with(
            PROJECT_ID,
            expected_thread_id=OFFICER_TID,
            hold=None,
        )
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
        db.set_project_officer_hold = AsyncMock()
        db.enqueue_session_wake_event = AsyncMock(return_value=True)
        monkeypatch.setattr(main, "postgres_db", db)
        monkeypatch.setattr(main, "_kick_officer_event_drain", MagicMock())

        await main._conclude_conference_if_any(_conference_row(status="ended"))
        db.set_project_officer_hold.assert_not_awaited()
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
    @pytest.fixture(autouse=True)
    def _runtime_authorization_is_healthy(self, monkeypatch):
        monkeypatch.setattr(
            main,
            "_maintain_officer_runtime_authorization",
            AsyncMock(return_value=SimpleNamespace(authorized=True)),
        )

    @pytest.mark.asyncio
    async def test_held_officer_still_maintains_runtime_authority(self, monkeypatch):
        held = _officer_row(hold={"kind": "conference", "thread_id": CONF_TID})
        db = SimpleNamespace()
        db.get_thread = AsyncMock(return_value=_conference_row(status="active"))
        monkeypatch.setattr(main, "postgres_db", db)
        maintain = AsyncMock()
        monkeypatch.setattr(main, "_maintain_officer_runtime_authorization", maintain)

        await main._officer_watchdog_check_one(held, SimpleNamespace())

        maintain.assert_awaited_once_with(held)

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
        # Legate walked away; attention sweep parked the conference. The
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
        db.set_project_officer_hold = AsyncMock(
            return_value={"thread": _officer_row(), "routes": []}
        )
        monkeypatch.setattr(main, "postgres_db", db)

        await main._officer_watchdog_check_one(held, SimpleNamespace())
        db.set_project_officer_hold.assert_awaited_once_with(
            PROJECT_ID,
            expected_thread_id=OFFICER_TID,
            hold=None,
        )


class TestReasoningLevelBridge:
    """The create-path effort bridge (session-create rebuilds config_override
    from validated fragments — an unbridged reasoning_level would be silently
    dropped, the trap that once ate the officer block)."""

    def test_accepts_known_levels(self):
        for level in ("low", "medium", "high", "xhigh", "max", "none"):
            assert main._validated_reasoning_level(level) == level

    def test_normalizes_case_and_whitespace(self):
        assert main._validated_reasoning_level("  XHigh ") == "xhigh"

    def test_rejects_garbage(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            main._validated_reasoning_level("ultra")


def _with_correctness_health(db):
    db.list_officer_job_preflights = AsyncMock(return_value=[])
    db.list_knowledge_materialization_health = AsyncMock(return_value=[])
    db.list_officer_floor_wake_outcomes = AsyncMock(return_value=[])
    return db


class TestOfficerSummaryEndpoint:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("is_admin", "role", "expected"),
        [
            (True, None, True),
            (False, "owner", True),
            (False, "editor", False),
            (False, "viewer", False),
        ],
    )
    async def test_management_capability_matches_mutation_authority(
        self, monkeypatch, is_admin, role, expected
    ):
        db = _with_correctness_health(SimpleNamespace())
        db.get_officer_thread_for_project = AsyncMock(return_value=None)
        db.get_or_create_project_officer = AsyncMock(
            return_value={
                "project_id": PROJECT_ID,
                "thread_id": None,
                "config_override": {},
                "communication_policy": {},
                "state": {},
                "incarnations": [],
            }
        )
        db.get_project_officer_lineage = AsyncMock(return_value=[])
        db.get_user_role_in_project = AsyncMock(return_value=role)
        monkeypatch.setattr(main, "postgres_db", db)
        monkeypatch.setattr(
            main,
            "require_project_member",
            AsyncMock(return_value=({"id": "user-1", "is_admin": is_admin}, {})),
        )
        monkeypatch.setattr(
            main, "_find_open_conference_thread", AsyncMock(return_value=None)
        )

        out = await main.get_project_officer_summary(MagicMock(), PROJECT_ID)

        assert out["can_manage"] is expected
        if is_admin:
            db.get_user_role_in_project.assert_not_awaited()
        else:
            db.get_user_role_in_project.assert_awaited_once_with(PROJECT_ID, "user-1")

    @pytest.mark.asyncio
    async def test_summary_shape(self, monkeypatch):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).date().isoformat()
        officer = _officer_row(officer_state={"ceiling_notice": today})

        class _Acq:
            async def __aenter__(self):
                return SimpleNamespace(
                    fetchval=AsyncMock(return_value=4),
                    fetch=AsyncMock(return_value=[{"slot": "line", "n": 1}]),
                )

            async def __aexit__(self, *a):
                return False

        incarnation = {
            "thread_id": str(uuid.uuid4()),
            "commissioned_at": "2026-07-01T00:00:00Z",
            "decommissioned_at": "2026-07-20T00:00:00Z",
            "reason": "retired",
        }
        db = _with_correctness_health(SimpleNamespace())
        db.get_officer_thread_for_project = AsyncMock(return_value=officer)
        db.get_or_create_project_officer = AsyncMock(
            return_value={
                "project_id": PROJECT_ID,
                "thread_id": OFFICER_TID,
                "config_override": {"officer": {"slots": {"line": {"count": 2}}}},
                "communication_policy": {
                    "worker_messages": "user_direct",
                    "officer_response_minutes": 15,
                },
                "state": {},
                "incarnations": [incarnation],
            }
        )
        db.get_project_officer_lineage = AsyncMock(return_value=[OFFICER_TID])
        db.get_pending_officer_timer = AsyncMock(
            return_value={"fire_at": "2026-07-30T05:00:00Z"}
        )
        db.acquire = lambda: _Acq()
        monkeypatch.setattr(main, "postgres_db", db)
        monkeypatch.setattr(
            main, "require_approved_user", AsyncMock(return_value={"id": "u"})
        )
        monkeypatch.setattr(
            main,
            "require_project_member",
            AsyncMock(return_value=({"id": "u", "is_admin": True}, {})),
        )
        monkeypatch.setattr(
            main,
            "_find_open_conference_thread",
            AsyncMock(return_value=_conference_row()),
        )

        out = await main.get_project_officer_summary(MagicMock(), PROJECT_ID)
        assert out["officer"]["thread_id"] == OFFICER_TID
        assert out["next_wake_at"] == "2026-07-30T05:00:00Z"
        assert out["pending_events"] == 4
        assert out["token_ceiling"]["deferred_today"] is True
        # Pages and digests are feed rows now (unified notification system):
        # the card lists them via GET /api/notifications?source_kind=thread,
        # so the summary carries neither a page budget nor a digest ring.
        assert "pages_today" not in out
        assert "digest" not in out
        assert out["conference"]["thread_id"] == CONF_TID
        # The post block (officer_post.md §8, partial O2 shape).
        assert out["commissioned"] is True
        assert out["held"] is None
        # Kit utilization is lineage-aware; the row's roster seeds the spec.
        assert out["kit"] == {"line": {"count": 2, "in_flight": 1}}
        assert out["communication_policy"]["worker_messages"] == "user_direct"
        assert out["incarnations"] == [incarnation]
        assert out["can_manage"] is True

    @pytest.mark.asyncio
    async def test_no_officer_renders_enable_prompt(self, monkeypatch):
        db = _with_correctness_health(SimpleNamespace())
        db.get_officer_thread_for_project = AsyncMock(return_value=None)
        # A vacant post whose last incarnation left a kit behind: the card's
        # provision form seeds from it (officer_post.md §8).
        db.get_or_create_project_officer = AsyncMock(
            return_value={
                "project_id": PROJECT_ID,
                "thread_id": None,
                "config_override": {"officer": {"slots": {"line": {"count": 2}}}},
                "communication_policy": {
                    "worker_messages": "user_direct",
                    "officer_response_minutes": 15,
                },
                "state": {},
                "incarnations": [],
            }
        )
        db.get_project_officer_lineage = AsyncMock(return_value=[])
        monkeypatch.setattr(main, "postgres_db", db)
        monkeypatch.setattr(
            main, "require_approved_user", AsyncMock(return_value={"id": "u"})
        )
        monkeypatch.setattr(
            main,
            "require_project_member",
            AsyncMock(return_value=({"id": "u", "is_admin": True}, {})),
        )
        monkeypatch.setattr(
            main, "_find_open_conference_thread", AsyncMock(return_value=None)
        )
        out = await main.get_project_officer_summary(MagicMock(), PROJECT_ID)
        # Vacancy keys off commissioned: false (O5 card contract); the officer
        # block is still present so the vacant editor seeds from the row —
        # live-only fields are null.
        assert out["commissioned"] is False
        assert out["officer"]["thread_id"] is None
        assert out["officer"]["status"] is None
        assert out["officer"]["slots"] == {"line": {"count": 2}}
        assert out["conference"] is None
        assert out["held"] is None
        assert out["kit"] == {"line": {"count": 2, "in_flight": 0}}
        assert out["incarnations"] == []
        assert out["communication_policy"]["officer_response_minutes"] == 15
        assert out["while_vacant"] == {"entries": [], "dropped": 0}
        assert out["can_manage"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stale_link_kind", ["ended", "missing"])
    async def test_stale_ended_or_missing_thread_link_reads_as_vacant(
        self, monkeypatch, stale_link_kind
    ):
        """OC-03 read surface: a link without a valid live joined thread must
        not claim ``commissioned: true`` over an empty officer block.

        ``get_officer_thread_for_project`` filters non-ended, so a stale link
        (an ended retire or a missing legacy row) yields officer=None while
        post.thread_id stays set in this synthetic fixture. The old
        ``bool(post['thread_id'])`` derivation reported a
        commissioned post the card could not render; commissioned must come
        from the live join, and the stale case renders exactly like vacancy.
        """
        db = _with_correctness_health(SimpleNamespace())
        # The live join found nothing — the linked thread is ended or missing.
        db.get_officer_thread_for_project = AsyncMock(return_value=None)
        db.get_or_create_project_officer = AsyncMock(
            return_value={
                "project_id": PROJECT_ID,
                # The stale link: non-null, but names a dead thread.
                "thread_id": OFFICER_TID,
                "config_override": {"officer": {"slots": {"line": {"count": 2}}}},
                "communication_policy": {
                    "worker_messages": "user_direct",
                    "officer_response_minutes": 15,
                },
                "state": {},
                "incarnations": [],
            }
        )
        db.get_project_officer_lineage = AsyncMock(return_value=[])
        monkeypatch.setattr(main, "postgres_db", db)
        monkeypatch.setattr(
            main, "require_approved_user", AsyncMock(return_value={"id": "u"})
        )
        monkeypatch.setattr(
            main,
            "require_project_member",
            AsyncMock(return_value=({"id": "u", "is_admin": True}, {})),
        )
        monkeypatch.setattr(
            main, "_find_open_conference_thread", AsyncMock(return_value=None)
        )
        out = await main.get_project_officer_summary(MagicMock(), PROJECT_ID)
        assert out["commissioned"] is False
        # And it renders as ordinary vacancy — editor seeded from the row,
        # live-only fields null — not as some third state.
        assert out["officer"]["thread_id"] is None
        assert out["officer"]["status"] is None
        assert out["kit"] == {"line": {"count": 2, "in_flight": 0}}


class TestConferenceBrainInheritance:
    """A conference is the officer's embodiment: it thinks with his model and
    effort unless the request says otherwise (officer_visibility_streamline.md
    §3.1 — closes conference live-fire F2)."""

    @staticmethod
    def _officer(llm, *, as_string=False):
        meta = {"config_override": {"officer": {"enabled": True}, "llm": llm}}
        return {"id": "off-1", "metadata": json.dumps(meta) if as_string else meta}

    def test_fills_model_and_reasoning_from_the_standing_officer(self):
        override = {"officer": {"conference": True}}
        got = main._inherit_conference_brain(
            override, self._officer({"model": "gpt-5.6-sol", "reasoning_level": "high"})
        )
        assert got == ["model", "reasoning_level"]
        assert override["llm"] == {"model": "gpt-5.6-sol", "reasoning_level": "high"}
        assert override["officer"] == {"conference": True}

    def test_request_provided_values_win(self):
        override = {"llm": {"model": "MiniMax-M3"}}
        got = main._inherit_conference_brain(
            override, self._officer({"model": "gpt-5.6-sol", "reasoning_level": "high"})
        )
        assert got == ["reasoning_level"]
        assert override["llm"] == {"model": "MiniMax-M3", "reasoning_level": "high"}

    def test_reads_jsonb_metadata_delivered_as_a_string(self):
        override = {}
        got = main._inherit_conference_brain(
            override, self._officer({"model": "gpt-5.6-sol"}, as_string=True)
        )
        assert got == ["model"]
        assert override["llm"]["model"] == "gpt-5.6-sol"

    def test_no_officer_or_brainless_officer_leaves_the_override_alone(self):
        override = {"officer": {"conference": True}}
        assert main._inherit_conference_brain(override, None) == []
        assert (
            main._inherit_conference_brain(override, {"id": "x", "metadata": {}}) == []
        )
        assert (
            main._inherit_conference_brain(
                override, {"id": "x", "metadata": "{not json"}
            )
            == []
        )
        assert "llm" not in override

    def test_ignores_blank_or_non_string_values(self):
        override = {}
        got = main._inherit_conference_brain(
            override, self._officer({"model": "   ", "reasoning_level": 3})
        )
        assert got == []
        assert "llm" not in override
