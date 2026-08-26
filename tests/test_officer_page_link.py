"""Officer page deep link — every page carries a way back to the session.

``_officer_session_link`` builds ``{COCKPIT_EXTERNAL_URL}/sessions/{thread_id}``
and ``_dispatch_officer_page`` appends it to the message body, so every caller
(the notify endpoint's page/digest urgencies, the recycler's respawn-failure
alert, the runtime-authorization incident) gets it for free. Unset base URL →
no link appended: a page without a link beats a page with a broken one.

Since the unified notification system (slice 1) a page is a feed row recorded
through ``notification_service.record()`` — ``officer_question`` at severity
``high`` — addressed to the thread owner with ``source_ref = thread:<id>``.
Delivery (email per the owner's preferences) is the notification system's
business; ``_dispatch_officer_page`` never chooses a channel. It returns the
recorded row's notification id (new or replayed) and ``None`` when there is
nobody to notify or the feed write failed — the notify endpoint turns that
``None`` into a 503 and echoes the id otherwise.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

import main
from services.notification_service import RecordResult

THREAD_ID = str(uuid.uuid4())
PROJECT_ID = str(uuid.uuid4())
BASE = "https://cockpit.example.com"


class TestOfficerSessionLink:
    def test_env_set_builds_link(self, monkeypatch):
        monkeypatch.setenv("COCKPIT_EXTERNAL_URL", BASE)
        assert main._officer_session_link(THREAD_ID) == f"{BASE}/sessions/{THREAD_ID}"

    def test_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("COCKPIT_EXTERNAL_URL", f"{BASE}/")
        assert main._officer_session_link(THREAD_ID) == f"{BASE}/sessions/{THREAD_ID}"

    def test_env_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("COCKPIT_EXTERNAL_URL", raising=False)
        assert main._officer_session_link(THREAD_ID) is None

    def test_env_blank_returns_none(self, monkeypatch):
        monkeypatch.setenv("COCKPIT_EXTERNAL_URL", "   ")
        assert main._officer_session_link(THREAD_ID) is None


@pytest.fixture
def thread():
    return {
        "user_id": "user-1",
        "project_id": PROJECT_ID,
        "config_name": "session_base",
        "title": "Centurion — Resavio",
    }


@pytest.fixture
def record(monkeypatch):
    mock = AsyncMock(
        return_value=RecordResult("n-1", True, {"in_app": True, "email": True})
    )
    monkeypatch.setattr(main.notification_service, "record", mock)
    return mock


class TestDispatchOfficerPageLink:
    @pytest.mark.asyncio
    async def test_appends_link_when_base_url_set(self, thread, record, monkeypatch):
        monkeypatch.setenv("COCKPIT_EXTERNAL_URL", BASE)
        notification_id = await main._dispatch_officer_page(
            thread, THREAD_ID, "Centurion needs you", "He fell asleep on watch."
        )
        assert notification_id == "n-1"
        body = record.call_args.kwargs["body"]
        assert body.startswith("He fell asleep on watch.")
        assert body.endswith(f"Open his log to reply: {BASE}/sessions/{THREAD_ID}")

    @pytest.mark.asyncio
    async def test_message_untouched_when_base_url_unset(
        self, thread, record, monkeypatch
    ):
        monkeypatch.delenv("COCKPIT_EXTERNAL_URL", raising=False)
        notification_id = await main._dispatch_officer_page(
            thread, THREAD_ID, "Centurion needs you", "He fell asleep on watch."
        )
        assert notification_id == "n-1"
        assert record.call_args.kwargs["body"] == "He fell asleep on watch."


class TestDispatchOfficerPageRecords:
    @pytest.mark.asyncio
    async def test_records_a_high_officer_question_for_the_owner(
        self, thread, record, monkeypatch
    ):
        monkeypatch.setenv("COCKPIT_EXTERNAL_URL", BASE)
        notification_id = await main._dispatch_officer_page(
            thread, THREAD_ID, "Centurion needs you", "He fell asleep on watch."
        )
        assert notification_id == "n-1"
        record.assert_awaited_once()
        kwargs = record.call_args.kwargs
        assert kwargs["recipient_id"] == "user-1"
        assert kwargs["category"] == "officer_question"
        assert kwargs["severity"] == "high"
        assert kwargs["subject"] == "Centurion needs you"
        # No job behind a page: the source is the officer's session thread.
        assert kwargs["source_kind"] == "thread"
        assert kwargs["source_id"] == THREAD_ID
        assert kwargs["action_params"] == {
            "thread_id": THREAD_ID,
            "project_id": PROJECT_ID,
        }
        assert kwargs["payload"]["config_name"] == "session_base"
        assert kwargs["payload"]["title"] == "Centurion — Resavio"

    @pytest.mark.asyncio
    async def test_default_subject(self, thread, record):
        notification_id = await main._dispatch_officer_page(
            thread, THREAD_ID, "", "Wake up."
        )
        assert notification_id == "n-1"
        assert record.call_args.kwargs["subject"] == "Your centurion needs you"

    @pytest.mark.asyncio
    async def test_dedup_key_collapses_identical_text_on_one_day(self, thread, record):
        await main._dispatch_officer_page(thread, THREAD_ID, "S", "Same text")
        await main._dispatch_officer_page(thread, THREAD_ID, "S", "Same text")
        await main._dispatch_officer_page(thread, THREAD_ID, "S", "Other text")
        keys = [c.kwargs["dedup_key"] for c in record.call_args_list]
        assert keys[0] == keys[1]
        assert keys[0] != keys[2]
        assert keys[0].startswith(f"officer_notify:{THREAD_ID}:")

    @pytest.mark.asyncio
    async def test_explicit_category_and_key_are_passed_through(self, thread, record):
        notification_id = await main._dispatch_officer_page(
            thread,
            THREAD_ID,
            "Officer authorization unavailable",
            "M",
            category="officer_runtime",
            dedup_key="officer_runtime_auth:claim-1",
        )
        assert notification_id == "n-1"
        assert record.call_args.kwargs["category"] == "officer_runtime"
        assert record.call_args.kwargs["dedup_key"] == "officer_runtime_auth:claim-1"

    @pytest.mark.asyncio
    async def test_low_severity_for_digest(self, thread, record):
        await main._dispatch_officer_page(thread, THREAD_ID, "S", "M", severity="low")
        assert record.call_args.kwargs["severity"] == "low"

    @pytest.mark.asyncio
    async def test_no_owner_means_nobody_to_notify(self, record):
        notification_id = await main._dispatch_officer_page(
            {"user_id": None}, THREAD_ID, "S", "M"
        )
        assert notification_id is None
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_feed_write_failure_is_reported_not_raised(self, thread, record):
        record.side_effect = RuntimeError("pool down")
        notification_id = await main._dispatch_officer_page(thread, THREAD_ID, "S", "M")
        assert notification_id is None
