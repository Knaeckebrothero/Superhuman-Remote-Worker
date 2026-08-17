"""Officer page deep link — every page carries a way back to the session.

``_officer_session_link`` builds ``{COCKPIT_EXTERNAL_URL}/sessions/{thread_id}``
and ``_dispatch_officer_page`` appends it to the message body, so both callers
(the notify endpoint's page urgency and the watchdog's respawn-failure alert)
get it for free. Unset base URL → no link appended: a page without a link
beats a page with a broken one.

The dispatch also persists the notification-center row (``message_log`` is the
bell's backing store): job_id NULL — there is no job behind a page, and the
jobs FK forbids the thread UUID — with thread_id carrying the session UUID so
the cockpit's action center can route "Open session log" (F4 addendum in
knowledge-base/knowledge/issues/officer_conference_live_fire_findings.md).
"""

import uuid
from unittest.mock import AsyncMock

import pytest

import main

THREAD_ID = str(uuid.uuid4())
BASE = "https://cockpit.example.com"


@pytest.fixture(autouse=True)
def bell_row(monkeypatch):
    """Hermetic message_log write — _dispatch_officer_page persists the bell
    row on every delivered page, and the real postgres_db has no pool here."""
    mock = AsyncMock(return_value={"id": "row-1"})
    monkeypatch.setattr(main.postgres_db, "log_message", mock)
    return mock


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


class TestDispatchOfficerPageLink:
    @pytest.fixture
    def thread(self):
        return {"user_id": "user-1", "config_name": "session_base"}

    @pytest.fixture
    def owner(self, monkeypatch):
        monkeypatch.setattr(
            main.postgres_db,
            "get_user",
            AsyncMock(
                return_value={
                    "email": "legate@example.com",
                    "display_name": "Legate",
                }
            ),
        )

    @pytest.fixture
    def dispatch(self, monkeypatch):
        mock = AsyncMock(return_value={"email": True})
        monkeypatch.setattr(main.notification_service, "dispatch", mock)
        return mock

    @pytest.mark.asyncio
    async def test_appends_link_when_base_url_set(
        self, thread, owner, dispatch, monkeypatch
    ):
        monkeypatch.setenv("COCKPIT_EXTERNAL_URL", BASE)
        ok = await main._dispatch_officer_page(
            thread, THREAD_ID, "Centurion needs you", "He fell asleep on watch."
        )
        assert ok is True
        body = dispatch.call_args.kwargs["message_md"]
        assert body.startswith("He fell asleep on watch.")
        assert body.endswith(f"Open his log to reply: {BASE}/sessions/{THREAD_ID}")

    @pytest.mark.asyncio
    async def test_message_untouched_when_base_url_unset(
        self, thread, owner, dispatch, monkeypatch
    ):
        monkeypatch.delenv("COCKPIT_EXTERNAL_URL", raising=False)
        ok = await main._dispatch_officer_page(
            thread, THREAD_ID, "Centurion needs you", "He fell asleep on watch."
        )
        assert ok is True
        assert dispatch.call_args.kwargs["message_md"] == "He fell asleep on watch."


class TestDispatchOfficerPageBellRow:
    @pytest.fixture
    def thread(self):
        return {"user_id": "user-1", "config_name": "session_base"}

    @pytest.fixture
    def owner(self, monkeypatch):
        monkeypatch.setattr(
            main.postgres_db,
            "get_user",
            AsyncMock(
                return_value={
                    "email": "legate@example.com",
                    "display_name": "Legate",
                }
            ),
        )

    @pytest.fixture
    def dispatch(self, monkeypatch):
        mock = AsyncMock(return_value={"email": True})
        monkeypatch.setattr(main.notification_service, "dispatch", mock)
        return mock

    @pytest.mark.asyncio
    async def test_persists_session_keyed_row(
        self, thread, owner, dispatch, bell_row, monkeypatch
    ):
        monkeypatch.setenv("COCKPIT_EXTERNAL_URL", BASE)
        ok = await main._dispatch_officer_page(
            thread, THREAD_ID, "Centurion needs you", "He fell asleep on watch."
        )
        assert ok is True
        bell_row.assert_awaited_once()
        kwargs = bell_row.call_args.kwargs
        # No job behind a page — the jobs FK forbids the thread UUID there.
        assert kwargs["job_id"] is None
        assert kwargs["thread_id"] == THREAD_ID
        assert kwargs["user_id"] == "user-1"
        assert kwargs["direction"] == "outbound"
        assert kwargs["subject"] == "Centurion needs you"
        assert kwargs["status"] == "sent"
        # Pre-link body: the cockpit card supplies the session route itself.
        assert kwargs["message"] == "He fell asleep on watch."
        assert dispatch.call_args.kwargs["message_md"].endswith(
            f"{BASE}/sessions/{THREAD_ID}"
        )

    @pytest.mark.asyncio
    async def test_default_subject_lands_in_row(
        self, thread, owner, dispatch, bell_row
    ):
        ok = await main._dispatch_officer_page(thread, THREAD_ID, "", "Wake up.")
        assert ok is True
        assert bell_row.call_args.kwargs["subject"] == "Your centurion needs you"
        assert dispatch.call_args.kwargs["subject"] == "Your centurion needs you"

    @pytest.mark.asyncio
    async def test_row_write_failure_does_not_fail_page(
        self, thread, owner, dispatch, bell_row
    ):
        bell_row.side_effect = RuntimeError("pool down")
        ok = await main._dispatch_officer_page(thread, THREAD_ID, "S", "M")
        assert ok is True

    @pytest.mark.asyncio
    async def test_no_row_when_dispatch_unavailable(
        self, thread, owner, bell_row, monkeypatch
    ):
        monkeypatch.setattr(
            main.notification_service,
            "dispatch",
            AsyncMock(return_value={"error": "NotificationService not initialized"}),
        )
        ok = await main._dispatch_officer_page(thread, THREAD_ID, "S", "M")
        assert ok is False
        bell_row.assert_not_awaited()
