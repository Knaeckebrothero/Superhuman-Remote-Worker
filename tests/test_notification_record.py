"""``NotificationService.record()`` / ``act()`` — unit level, every collaborator
stubbed (the ``tests/test_officer_guards.py`` seam: build ``__new__`` and
replace the private methods).

What these pin (unified notification system, D10):
  * the feed row is written before any channel is tried, and the SSE frame
    fires exactly once — never on a replay;
  * every channel send is claim-before-send, so a replayed effect re-attempts
    only what never got a ``sent`` claim;
  * a channel failure settles ``failed`` and never loses the notification;
  * quiet hours defer an immediate step to the window's end instead of
    dropping it (slice 2), and the class's deferred steps are planned with
    the row (D5: the delay is the step's ``due_at``), with the wait taken
    from the project officer's window when one is live, else the
    recipient's own escalation minutes, else 5.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import notification_catalog as cat
from services.notification_service import (
    ActionNotDeclared,
    ActionUnregistered,
    NotificationNotFound,
    NotificationService,
    RecordResult,
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
    svc._persist_notification = AsyncMock(
        side_effect=lambda row, steps=None: (row["id"], inserted)
    )
    svc._claim_delivery = AsyncMock(return_value=claim)
    svc._settle_delivery = AsyncMock()
    svc._record_suppressed = AsyncMock()
    svc._defer_steps = AsyncMock(return_value=1)
    svc._get_user = AsyncMock(
        return_value={"id": USER, "email": "legate@example.org", "display_name": "L"}
    )
    svc._get_user_channels = AsyncMock(return_value=channels or {"email": True})
    svc._get_user_settings = AsyncMock(return_value={})
    svc._is_in_quiet_hours = MagicMock(return_value=quiet)
    svc.next_quiet_hours_end = MagicMock(
        return_value=datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)
    )
    svc._resolve_delay_minutes = AsyncMock(return_value=5)
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
    # Default to a `high` category (immediate email); the `normal`
    # review_queue class is the deferred case and is exercised explicitly.
    kwargs = dict(
        recipient_id=USER,
        category="budget_exceeded",
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
        # `high` mails immediately; `review_queue` (normal) is the deferred
        # case and has its own tests below.
        svc = _service()
        result = await _record(svc, category="budget_exceeded")

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
        await _record(svc, category="review_queue", action_params={"job_id": "job-1"})
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
    async def test_quiet_hours_defer_an_immediate_step_to_the_window_end(self):
        # `high` mails immediately — unless the recipient is in quiet hours,
        # in which case the step is parked (not dropped, not queued into the
        # legacy digest) until the window ends, still gated on not_resolved.
        svc = _service(quiet=True)
        result = await _record(svc, category="budget_exceeded")
        assert result.deliveries["deferred_until"] == "2026-08-27T06:00:00+00:00"
        svc._email_service.send_notification_email.assert_not_awaited()
        svc._claim_delivery.assert_not_awaited()
        svc._record_suppressed.assert_not_awaited()
        svc._defer_steps.assert_awaited_once()
        nid, steps = svc._defer_steps.call_args.args
        assert nid == result.notification_id
        assert [s["step_kind"] for s in steps] == ["email"]
        assert steps[0]["step_index"] >= cat.DEFERRED_STEP_INDEX_BASE
        assert steps[0]["conditions"] == ["not_resolved"]
        assert steps[0]["due_at"] == datetime(2026, 8, 27, 6, 0, tzinfo=timezone.utc)

        # A replay defers again; the insert is idempotent per step index.
        replay = _service(quiet=True, inserted=False)
        await _record(replay, category="budget_exceeded")
        replay._defer_steps.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_critical_crosses_quiet_hours(self):
        svc = _service(quiet=True)
        result = await _record(svc, category="incident")
        assert "deferred_until" not in result.deliveries
        svc._email_service.send_notification_email.assert_awaited_once()
        svc._defer_steps.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_category_channel_cell_overrides_the_channel_default(self):
        svc = _service(channels={"email": True})
        svc._get_user_settings = AsyncMock(
            return_value={
                "communication": {"categories": {"budget_exceeded": {"email": False}}}
            }
        )
        result = await _record(svc, category="budget_exceeded")
        svc._email_service.send_notification_email.assert_not_awaited()
        assert svc._record_suppressed.call_args.args[1:] == ("email", "preference")
        assert "email" not in result.deliveries

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


class TestDeferredSteps:
    """D5/D6: a `normal` row mails later, not now — the class's deferred
    steps are planned with the row and written in its transaction."""

    @pytest.mark.asyncio
    async def test_normal_plans_steps_instead_of_mailing(self):
        svc = _service()
        before = datetime.now(timezone.utc)
        result = await _record(svc, category="review_queue")
        svc._email_service.send_notification_email.assert_not_awaited()
        svc._claim_delivery.assert_not_awaited()
        steps = svc._persist_notification.call_args.kwargs["steps"]
        assert [s["step_kind"] for s in steps] == [
            "email",
            "ntfy",
            "slack_webhook",
            "discord_webhook",
        ]
        email = steps[0]
        assert email["conditions"] == ["not_seen", "not_resolved"]
        assert email["batch_key"] == "review_queue"
        assert email["step_index"] == 0
        # 5 minutes (the stubbed resolution), rounded up to the 15-min bucket:
        # never earlier than the delay, at most one window later.
        assert before + timedelta(minutes=5) <= email["due_at"]
        assert email["due_at"] <= before + timedelta(minutes=20, seconds=1)
        assert email["due_at"].minute % 15 == 0
        assert result.deliveries["scheduled"]["email"] == email["due_at"].isoformat()
        assert result.deliveries["in_app"] is True

    @pytest.mark.asyncio
    async def test_replay_hands_the_same_plan_to_the_idempotent_insert(self):
        svc = _service(inserted=False)
        await _record(svc, category="review_queue")
        assert svc._persist_notification.call_args.kwargs["steps"]
        svc._broadcast_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_officer_recipient_gets_no_steps(self):
        svc = _service()
        await _record(svc, category="review_queue", recipient_kind="officer")
        assert svc._persist_notification.call_args.kwargs["steps"] is None
        svc._resolve_delay_minutes.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_immediate_classes_plan_nothing(self):
        svc = _service()
        await _record(svc, category="incident")
        assert svc._persist_notification.call_args.kwargs["steps"] is None
        assert "scheduled" not in (await _record(svc, category="incident")).deliveries


class TestDelayResolution:
    """The wait is the officer's window when a live officer owns the review;
    otherwise the recipient's own setting; otherwise 5 minutes."""

    def _svc(self, *, job=None, post=None, officer=None):
        svc = _service()
        svc._resolve_delay_minutes = NotificationService._resolve_delay_minutes.__get__(
            svc
        )
        svc._officer_response_minutes = (
            NotificationService._officer_response_minutes.__get__(svc)
        )
        svc._db.get_job = AsyncMock(return_value=job)
        svc._db.get_project_officer = AsyncMock(return_value=post)
        svc._db.get_officer_thread_for_project = AsyncMock(return_value=officer)
        return svc

    def _row(self, **overrides):
        row = {"source_kind": "job", "source_id": "job-1"}
        row.update(overrides)
        return row

    @pytest.mark.asyncio
    async def test_live_officer_window_wins(self):
        svc = self._svc(
            job={"id": "job-1", "project_id": "p-1"},
            post={"communication_policy": {"officer_response_minutes": 30}},
            officer={"id": "t-1", "metadata": {"officer": {}}},
        )
        assert await svc._resolve_delay_minutes(self._row(), {}) == 30

    @pytest.mark.asyncio
    async def test_policy_bounds_are_clamped_and_json_strings_parse(self):
        svc = self._svc(
            job={"id": "job-1", "project_id": "p-1"},
            post={"communication_policy": '{"officer_response_minutes": 999}'},
            officer={"id": "t-1"},
        )
        assert await svc._resolve_delay_minutes(self._row(), {}) == 120

    @pytest.mark.asyncio
    async def test_held_officer_falls_back_to_the_recipient_setting(self):
        svc = self._svc(
            job={"id": "job-1", "project_id": "p-1"},
            post={"communication_policy": {"officer_response_minutes": 30}},
            officer={
                "id": "t-1",
                # the hold stamp lives at metadata.config_override.officer.hold
                "metadata": {
                    "config_override": {"officer": {"hold": {"kind": "maintenance"}}}
                },
            },
        )
        settings = {"communication": {"escalation_minutes": 12}}
        assert await svc._resolve_delay_minutes(self._row(), settings) == 12

    @pytest.mark.asyncio
    async def test_no_project_no_post_or_vacant_means_default_five(self):
        assert (
            await self._svc(job={"id": "job-1"})._resolve_delay_minutes(self._row(), {})
            == cat.NO_OFFICER_DELAY_MINUTES
        )
        assert (
            await self._svc(
                job={"id": "job-1", "project_id": "p-1"}
            )._resolve_delay_minutes(self._row(), {})
            == cat.NO_OFFICER_DELAY_MINUTES
        )
        vacant = self._svc(
            job={"id": "job-1", "project_id": "p-1"},
            post={"communication_policy": {}},
            officer=None,
        )
        assert (
            await vacant._resolve_delay_minutes(self._row(), {})
            == cat.NO_OFFICER_DELAY_MINUTES
        )

    @pytest.mark.asyncio
    async def test_bad_recipient_setting_is_ignored(self):
        svc = self._svc(job=None)
        for bad in (0, -1, True, "7", 100000):
            settings = {"communication": {"escalation_minutes": bad}}
            assert (
                await svc._resolve_delay_minutes(self._row(), settings)
                == cat.NO_OFFICER_DELAY_MINUTES
            )

    @pytest.mark.asyncio
    async def test_lookup_failure_degrades_to_the_default(self):
        svc = self._svc()
        svc._db.get_job = AsyncMock(side_effect=RuntimeError("db down"))
        assert (
            await svc._resolve_delay_minutes(self._row(), {})
            == cat.NO_OFFICER_DELAY_MINUTES
        )

    @pytest.mark.asyncio
    async def test_non_job_sources_never_consult_the_officer(self):
        svc = self._svc()
        assert (
            await svc._resolve_delay_minutes(
                self._row(source_kind="thread", source_id="t-1"), {}
            )
            == cat.NO_OFFICER_DELAY_MINUTES
        )
        svc._db.get_job.assert_not_awaited()


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


class TestAgentMessage:
    """``record_agent_message`` — the worker→owner producer as a shape over
    ``record()``: normal by default, high when blocking, reply-routable by
    mail, ledger-linked, and addressable to a contact who has no user row."""

    def _svc(self):
        svc = NotificationService.__new__(NotificationService)
        svc._available = True
        svc.record = AsyncMock(
            return_value=RecordResult(
                "n-1", True, {"in_app": True, "email": True, "email_message_id": "<m>"}
            )
        )
        return svc

    async def _call(self, svc, **overrides):
        kwargs = dict(
            user_id=USER,
            job={"description": "Publish the demo", "config_name": "worker_base"},
            job_id="job-1",
            thread_id="abc123",
            sequence=3,
            subject="Need input",
            message_md="Which colour?",
        )
        kwargs.update(overrides)
        return await svc.record_agent_message(**kwargs)

    @pytest.mark.asyncio
    async def test_shape(self):
        svc = self._svc()
        result = await self._call(svc)
        kw = svc.record.await_args.kwargs
        assert kw["category"] == "agent_message"
        assert kw["severity"] is None  # the class default: normal → deferred
        assert kw["dedup_key"] == "message:abc123:3"
        assert (kw["source_kind"], kw["source_id"]) == ("message_thread", "abc123")
        assert kw["action_params"] == {"job_id": "job-1", "thread_id": "abc123"}
        payload = kw["payload"]
        assert payload["reply_routing"] == {"job_id": "job-1", "thread_id": "abc123"}
        assert payload["blocking"] is False and "deliver_to" not in payload
        assert result.as_dispatch() == {
            "in_app": True,
            "email": True,
            "email_message_id": "<m>",
            "notification_id": "n-1",
        }

    @pytest.mark.asyncio
    async def test_blocking_is_high_and_carries_the_ledger_id(self):
        svc = self._svc()
        await self._call(svc, blocking=True, message_log_id="ml-9")
        kw = svc.record.await_args.kwargs
        assert kw["severity"] == "high"
        assert kw["payload"]["blocking"] is True
        assert kw["payload"]["message_log_id"] == "ml-9"

    @pytest.mark.asyncio
    async def test_explicit_severity_key_and_contact_override(self):
        svc = self._svc()
        await self._call(
            svc,
            sequence=None,
            severity="high",
            dedup_key="route_escalation:r1:officer_sla_expired",
            reason_line="officer_sla_expired",
            deliver_to=("contact@example.org", "Contact"),
        )
        kw = svc.record.await_args.kwargs
        assert kw["severity"] == "high"
        assert kw["dedup_key"] == "route_escalation:r1:officer_sla_expired"
        assert kw["payload"]["deliver_to"] == {
            "email": "contact@example.org",
            "name": "Contact",
        }
        assert kw["payload"]["reason_line"] == "officer_sla_expired"

    @pytest.mark.asyncio
    async def test_no_sequence_gets_a_random_key(self):
        svc = self._svc()
        await self._call(svc, sequence=None)
        first = svc.record.await_args.kwargs["dedup_key"]
        await self._call(svc, sequence=None)
        assert first != svc.record.await_args.kwargs["dedup_key"]
        assert first.startswith("message:abc123:")


class TestReplyRoutingAndLedger:
    """An agent message's mail must stay answerable: the Reply-To is the
    IMAP sub-address for the thread, and the sent Message-ID is stamped onto
    the ledger row so an In-Reply-To reply resolves to the thread."""

    def _svc(self):
        svc = _service()
        svc._email_service.reply_address = MagicMock(
            side_effect=lambda job_id, thread_id: f"agent+{job_id[:8]}+{thread_id}@srw"
        )
        svc._db.set_message_email_id = AsyncMock(return_value=True)
        return svc

    @pytest.mark.asyncio
    async def test_reply_to_and_ledger_stamp_on_an_immediate_send(self):
        svc = self._svc()
        await svc.record_agent_message(
            user_id=USER,
            job={},
            job_id="job-1",
            thread_id="abc123",
            sequence=1,
            subject="s",
            message_md="m",
            blocking=True,  # high → immediate email
            message_log_id="ml-1",
        )
        send = svc._email_service.send_notification_email
        send.assert_awaited_once()
        assert send.call_args.kwargs["reply_to"] == "agent+job-1+abc123@srw"
        svc._db.set_message_email_id.assert_awaited_once_with("ml-1", "<msg@srw>")

    @pytest.mark.asyncio
    async def test_other_categories_have_no_reply_lane(self):
        svc = self._svc()
        await _record(svc, category="budget_exceeded")
        send = svc._email_service.send_notification_email
        assert send.call_args.kwargs["reply_to"] is None
        svc._db.set_message_email_id.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_contact_override_mails_the_contact_not_the_owner(self):
        svc = self._svc()
        await svc.record_agent_message(
            user_id=USER,
            job={},
            job_id="job-1",
            thread_id="abc123",
            sequence=1,
            subject="s",
            message_md="m",
            blocking=True,
            deliver_to=("contact@example.org", "Contact"),
        )
        send = svc._email_service.send_notification_email
        assert send.call_args.kwargs["to"] == "contact@example.org"
        assert send.call_args.kwargs["to_name"] == "Contact"
        svc._claim_delivery.assert_awaited_once_with(
            svc._persist_notification.call_args.args[0]["id"],
            "email",
            address="contact@example.org",
        )

    @pytest.mark.asyncio
    async def test_a_batch_splits_by_override_address(self):
        svc = self._svc()
        svc._claim_delivery = AsyncMock(side_effect=lambda *a, **k: "c")
        members = [
            {
                "id": 1,
                "notification_id": "n-1",
                "step_index": 0,
                "recipient_id": USER,
                "subject": "to owner",
                "body": "b",
                "category": "agent_message",
                "payload": {},
            },
            {
                "id": 2,
                "notification_id": "n-2",
                "step_index": 0,
                "recipient_id": USER,
                "subject": "to contact",
                "body": "b",
                "category": "agent_message",
                "payload": {"deliver_to": {"email": "c@x", "name": "C"}},
            },
        ]
        out = await svc.send_step_group(members, channel="email")
        send = svc._email_service.send_notification_email
        assert send.await_count == 2
        assert {c.kwargs["to"] for c in send.call_args_list} == {
            "legate@example.org",
            "c@x",
        }
        assert sorted(out["attempted"]) == [1, 2] and out["ok"] is True
