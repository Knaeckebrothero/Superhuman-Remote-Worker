"""``NotificationService.record()`` / ``act()`` — unit level, every collaborator
stubbed (the ``tests/test_officer_guards.py`` seam: build ``__new__`` and
replace the private methods).

What these pin (unified notification system, D10):
  * the feed row is written before any channel is tried, and the SSE frame
    fires exactly once — never on a replay;
  * every channel send is claim-before-send, so a replayed effect re-attempts
    only what never got a ``sent`` claim;
  * a channel failure settles ``failed`` and never loses the notification;
  * quiet hours suppress (and, while the legacy digest still exists, queue)
    only from the inserting call.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services import notification_catalog as cat
from services.notification_service import (
    ActionNotDeclared,
    ActionUnregistered,
    NotificationNotFound,
    NotificationService,
)

USER = "11111111-1111-1111-1111-111111111111"


def _service(
    *,
    inserted=True,
    claim="claim-1",
    channels=None,
    quiet=False,
    email=True,
):
    svc = NotificationService.__new__(NotificationService)
    svc._available = True
    svc._db = MagicMock()
    svc._notification_feed = None
    svc._cockpit_url = "https://cockpit"
    svc._transports = {}
    svc._persist_notification = AsyncMock(side_effect=lambda row: (row["id"], inserted))
    svc._claim_delivery = AsyncMock(return_value=claim)
    svc._settle_delivery = AsyncMock()
    svc._record_suppressed = AsyncMock()
    svc._get_user = AsyncMock(
        return_value={"id": USER, "email": "legate@example.org", "display_name": "L"}
    )
    svc._get_user_channels = AsyncMock(return_value=channels or {"email": True})
    svc._get_user_settings = AsyncMock(return_value={})
    svc._is_in_quiet_hours = MagicMock(return_value=quiet)
    svc._queue_notification = AsyncMock()
    svc._broadcast_notification = MagicMock()
    svc._broadcast_update = MagicMock()
    if email:
        svc._email_service = MagicMock()
        svc._email_service.send_notification_email = AsyncMock(
            return_value=(True, "<msg@srw>")
        )
    else:
        svc._email_service = None
    return svc


async def _record(svc, **overrides):
    kwargs = dict(
        recipient_id=USER,
        category="review_queue",
        dedup_key="freeze_notification:cmd-1",
        subject="Job abc completed — review required",
        body="**Job** …",
        source_kind="job",
        source_id="job-1",
        action_params={"job_id": "job-1"},
    )
    kwargs.update(overrides)
    return await svc.record(**kwargs)


class TestRecord:
    @pytest.mark.asyncio
    async def test_inserted_row_broadcasts_once_and_mails_via_claim(self):
        svc = _service()
        result = await _record(svc)

        assert result.inserted is True
        assert result.notification_id == str(
            cat.notification_id("user", USER, "freeze_notification:cmd-1")
        )
        svc._broadcast_notification.assert_called_once()
        svc._claim_delivery.assert_awaited_once_with(
            result.notification_id, "email", address="legate@example.org"
        )
        send = svc._email_service.send_notification_email
        send.assert_awaited_once()
        assert (
            send.call_args.kwargs["cockpit_path"]
            == f"/inbox?n={result.notification_id}"
        )
        svc._settle_delivery.assert_awaited_once_with(
            "claim-1", state="sent", provider_msg_id="<msg@srw>", error=None
        )
        assert result.deliveries["email"] is True
        assert result.deliveries["in_app"] is True

    @pytest.mark.asyncio
    async def test_declared_actions_carry_the_call_params(self):
        svc = _service()
        await _record(svc, action_params={"job_id": "job-1"})
        row = svc._persist_notification.call_args.args[0]
        assert [a["type"] for a in row["actions"]] == ["approve", "resume", "open"]
        assert all(a["params"] == {"job_id": "job-1"} for a in row["actions"])
        assert row["severity"] == "normal"

    @pytest.mark.asyncio
    async def test_replay_broadcasts_nothing_but_still_claims(self):
        svc = _service(inserted=False)
        result = await _record(svc)
        assert result.inserted is False
        svc._broadcast_notification.assert_not_called()
        # The claim ledger, not the insert, decides whether to send.
        svc._claim_delivery.assert_awaited_once()
        svc._email_service.send_notification_email.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lost_claim_means_already_delivered(self):
        svc = _service(inserted=False, claim=None)
        result = await _record(svc)
        svc._email_service.send_notification_email.assert_not_awaited()
        svc._settle_delivery.assert_not_awaited()
        assert result.deliveries["email"] == "already_delivered"

    @pytest.mark.asyncio
    async def test_email_exception_settles_failed_and_returns(self):
        svc = _service()
        svc._email_service.send_notification_email = AsyncMock(
            side_effect=OSError("smtp down")
        )
        result = await _record(svc)
        svc._settle_delivery.assert_awaited_once()
        assert svc._settle_delivery.call_args.kwargs["state"] == "failed"
        assert "smtp down" in svc._settle_delivery.call_args.kwargs["error"]
        assert result.deliveries["email"] is False
        assert result.inserted is True  # the row survived the channel failure

    @pytest.mark.asyncio
    async def test_send_returning_false_is_a_failed_delivery(self):
        svc = _service()
        svc._email_service.send_notification_email = AsyncMock(
            return_value=(False, None)
        )
        result = await _record(svc)
        assert svc._settle_delivery.call_args.kwargs["state"] == "failed"
        assert result.deliveries["email"] is False

    @pytest.mark.asyncio
    async def test_quiet_hours_suppress_and_queue_only_when_inserted(self):
        svc = _service(quiet=True)
        result = await _record(svc)
        assert result.deliveries["queued"] is True
        svc._email_service.send_notification_email.assert_not_awaited()
        svc._record_suppressed.assert_awaited_once()
        assert svc._record_suppressed.call_args.args[1:] == ("email", "quiet_hours")
        svc._queue_notification.assert_awaited_once()

        replay = _service(quiet=True, inserted=False)
        await _record(replay)
        replay._record_suppressed.assert_not_awaited()
        replay._queue_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_critical_crosses_quiet_hours(self):
        svc = _service(quiet=True)
        result = await _record(svc, category="incident")
        assert result.deliveries.get("queued") is False
        svc._email_service.send_notification_email.assert_awaited_once()
        svc._queue_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_channel_preference_off_is_recorded_as_suppressed(self):
        svc = _service(channels={"email": False})
        result = await _record(svc)
        svc._email_service.send_notification_email.assert_not_awaited()
        svc._record_suppressed.assert_awaited_once()
        assert svc._record_suppressed.call_args.args[1:] == ("email", "preference")
        assert "email" not in result.deliveries

    @pytest.mark.asyncio
    async def test_low_severity_is_in_app_only(self):
        svc = _service()
        result = await _record(svc, category="officer_question", severity="low")
        svc._claim_delivery.assert_not_awaited()
        assert result.deliveries == {"in_app": True}

    @pytest.mark.asyncio
    async def test_no_email_address_is_suppressed_not_lost(self):
        svc = _service()
        svc._get_user = AsyncMock(return_value={"id": USER, "email": None})
        result = await _record(svc)
        assert result.inserted is True
        svc._record_suppressed.assert_awaited_once()
        assert svc._record_suppressed.call_args.args[1:] == ("email", "no_email")

    @pytest.mark.asyncio
    async def test_officer_recipient_gets_no_channels(self):
        svc = _service()
        result = await _record(svc, recipient_kind="officer")
        assert result.deliveries == {"in_app": True}
        svc._claim_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_category_is_loud(self):
        svc = _service()
        with pytest.raises(ValueError, match="unknown notification category"):
            await _record(svc, category="teleport")
        svc._persist_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_source_ref_must_be_whole(self):
        svc = _service()
        with pytest.raises(ValueError):
            await _record(svc, source_kind="job", source_id=None)

    @pytest.mark.asyncio
    async def test_missing_dedup_key_is_rejected(self):
        svc = _service()
        with pytest.raises(ValueError, match="dedup_key"):
            await _record(svc, dedup_key="")


def _row(**overrides):
    row = {
        "id": "n-1",
        "recipient_kind": "user",
        "recipient_id": USER,
        "category": "incident",
        "severity": "critical",
        "subject": "s",
        "body": "b",
        "source_kind": "job",
        "source_id": "job-1",
        "actions": [
            {
                "type": "open",
                "label_key": "notifications.actions.openJob",
                "params": {"job_id": "job-1"},
            }
        ],
        "payload": {},
        "created_at": None,
        "seen_at": None,
        "read_at": None,
        "interacted_at": None,
        "resolved_at": None,
        "resolved_by": None,
        "archived_at": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def handlers():
    saved = dict(cat._ACTION_HANDLERS)
    yield cat._ACTION_HANDLERS
    cat._ACTION_HANDLERS.clear()
    cat._ACTION_HANDLERS.update(saved)


class TestAct:
    def _svc(self, row):
        svc = _service()
        svc._db.get_notification = AsyncMock(return_value=row)
        svc._db.stamp_notification_interacted = AsyncMock(
            return_value={**row, "interacted_at": "2026-08-26T09:00:00+00:00"}
        )
        svc._db.resolve_notification = AsyncMock(
            return_value={
                **row,
                "resolved_at": "2026-08-26T09:00:01+00:00",
                "resolved_by": "user:" + USER,
            }
        )
        return svc

    @pytest.mark.asyncio
    async def test_foreign_row_is_not_found(self, handlers):
        svc = self._svc(_row(recipient_id="someone-else"))
        with pytest.raises(NotificationNotFound):
            await svc.act(
                notification_id="n-1", user={"id": USER}, action_type="open", params={}
            )

    @pytest.mark.asyncio
    async def test_undeclared_action_is_rejected(self, handlers):
        svc = self._svc(_row())
        with pytest.raises(ActionNotDeclared):
            await svc.act(
                notification_id="n-1",
                user={"id": USER},
                action_type="approve",
                params={},
            )

    @pytest.mark.asyncio
    async def test_declared_but_unregistered_is_loud(self, handlers):
        handlers.pop(("incident", "open"), None)
        svc = self._svc(_row())
        with pytest.raises(ActionUnregistered):
            await svc.act(
                notification_id="n-1", user={"id": USER}, action_type="open", params={}
            )

    @pytest.mark.asyncio
    async def test_handler_runs_with_server_params_winning(self, handlers):
        seen = {}

        @cat.register_action("incident", "open")
        async def _open(ctx):
            seen.update(ctx.params)
            return cat.ActionResult(result={"navigate": "/jobs/job-1"})

        svc = self._svc(_row())
        outcome = await svc.act(
            notification_id="n-1",
            user={"id": USER},
            action_type="open",
            params={"job_id": "forged", "note": "hi"},
        )
        assert seen == {"job_id": "job-1", "note": "hi"}
        assert outcome["result"] == {"navigate": "/jobs/job-1"}
        svc._db.stamp_notification_interacted.assert_awaited_once_with("n-1")
        svc._db.resolve_notification.assert_not_awaited()
        svc._broadcast_update.assert_called_once()
        assert svc._broadcast_update.call_args.args[0] == USER
        assert outcome["notification"]["interacted_at"] == "2026-08-26T09:00:00+00:00"

    @pytest.mark.asyncio
    async def test_result_resolve_stamps_resolution(self, handlers):
        @cat.register_action("incident", "open")
        async def _open(ctx):
            return cat.ActionResult(result={}, resolve=True)

        svc = self._svc(_row())
        outcome = await svc.act(
            notification_id="n-1", user={"id": USER}, action_type="open", params={}
        )
        svc._db.resolve_notification.assert_awaited_once_with(
            "n-1", resolved_by="user:" + USER
        )
        assert outcome["notification"]["resolved_by"] == "user:" + USER


class TestResolveSource:
    @pytest.mark.asyncio
    async def test_stamps_every_open_row_and_broadcasts_per_recipient(self):
        svc = _service()
        svc._db.resolve_notifications_by_source = AsyncMock(
            return_value=[
                _row(id="a", recipient_id="u1", resolved_at=None, resolved_by="user:x"),
                _row(id="b", recipient_id="u2", resolved_at=None, resolved_by="user:x"),
            ]
        )
        ids = await svc.resolve_source("job", "job-1", resolved_by="user:x")
        assert ids == ["a", "b"]
        assert [c.args[0] for c in svc._broadcast_update.call_args_list] == ["u1", "u2"]

    @pytest.mark.asyncio
    async def test_db_failure_is_swallowed(self):
        svc = _service()
        svc._db.resolve_notifications_by_source = AsyncMock(
            side_effect=RuntimeError("down")
        )
        assert await svc.resolve_source("job", "job-1", resolved_by="system:x") == []


class TestLegacyDispatchUntouched:
    """Slice 1 leaves ``dispatch()`` alone; the officer-guards seam must hold."""

    @pytest.mark.asyncio
    async def test_dispatch_still_queues_in_quiet_hours(self):
        svc = NotificationService.__new__(NotificationService)
        svc._available = True
        svc._get_user_channels = AsyncMock(return_value={"email": False})
        svc._get_user_settings = AsyncMock(return_value={})
        svc._is_in_quiet_hours = MagicMock(return_value=True)
        svc._queue_notification = AsyncMock()
        svc._broadcast_sse = AsyncMock()
        svc._cockpit_url = "https://cockpit"
        svc._email_service = None
        svc._transports = {}
        results = await svc.dispatch(
            user_id="u",
            job_id="j" * 36,
            subject="s",
            message_md="m",
            job_description="d",
            config_name="c",
        )
        assert results["queued"] is True
        svc._queue_notification.assert_awaited_once()
