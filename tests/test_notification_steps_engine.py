"""The escalate-on-timeout sweeper (``services/notification_steps.py``) and
the send half it calls back into (``NotificationService.send_step_group``),
with the database and the providers stubbed.

What these pin (unified notification system, D5/D6/D8/D10):
  * conditions are evaluated AT DUE TIME — a row seen or a source settled
    after the step was written skips it, and the live probe outranks the
    row's own ``resolved_at``;
  * preferences and quiet hours apply at the channel step, never earlier;
    quiet hours defer (claim released, attempt not counted), critical
    crosses them;
  * one message per (recipient, channel, batch key): three review items
    become one digest with three delivery claims sharing a batch id;
  * a provider failure walks the backoff ladder and then fails loudly; a
    member whose channel already went out is left alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from services import notification_catalog as cat
from services import notification_steps as engine
from services.notification_service import NotificationService

USER = "11111111-1111-1111-1111-111111111111"
NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _step(**overrides):
    step = {
        "id": 1,
        "notification_id": "n-1",
        "step_index": 0,
        "step_kind": "email",
        "due_at": NOW - timedelta(minutes=1),
        "conditions": ["not_seen", "not_resolved"],
        "batch_key": "review_queue",
        "state": "pending",
        "attempt": 1,
        "recipient_kind": "user",
        "recipient_id": USER,
        "category": "review_queue",
        "severity": "normal",
        "subject": "Job 1 completed — review required",
        "body": "first line\nsecond line",
        "source_kind": "job",
        "source_id": "job-1",
        "payload": {"job_id": "job-1"},
        "seen_at": None,
        "read_at": None,
        "resolved_at": None,
        "archived_at": None,
    }
    step.update(overrides)
    return step


class FakeDB:
    def __init__(self, steps):
        self.steps = steps
        self.settled: list[tuple[list, str, str | None]] = []
        self.deferred: list[tuple[list, datetime, str | None]] = []
        self.retried: list[tuple[list, datetime, str | None]] = []
        self.claim_kwargs = None

    async def claim_due_notification_steps(self, **kwargs):
        self.claim_kwargs = kwargs
        return [dict(s) for s in self.steps]

    async def settle_notification_steps(self, ids, *, state, detail=None):
        self.settled.append((list(ids), state, detail))
        return len(ids)

    async def defer_notification_steps(self, ids, *, due_at, detail=None):
        self.deferred.append((list(ids), due_at, detail))
        return len(ids)

    async def retry_notification_steps(self, ids, *, due_at, detail=None):
        self.retried.append((list(ids), due_at, detail))
        return len(ids)

    def states(self):
        out = {}
        for ids, state, detail in self.settled:
            for i in ids:
                out[i] = (state, detail)
        return out


class FakeService:
    def __init__(
        self,
        *,
        settings=None,
        channels=None,
        deliverable=True,
        resolved=False,
        outcome=None,
    ):
        self.settings = settings or {}
        self.channels = channels or {"email": True}
        self.deliverable = deliverable
        self.resolved = resolved
        self.outcome = outcome
        self.probed: list[tuple[str, str]] = []
        self.suppressed: list[tuple[str, str, str]] = []
        self.groups: list[tuple[str, list]] = []
        self.batch_ids: list[str] = []

    async def _get_user_settings(self, recipient_id):
        return self.settings

    async def _get_user_channels(self, recipient_id):
        return self.channels

    async def _source_resolved(self, kind, sid):
        self.probed.append((kind, str(sid)))
        return self.resolved

    def _channel_deliverable(self, channel):
        return self.deliverable

    async def _record_suppressed(self, nid, channel, reason):
        self.suppressed.append((nid, channel, reason))

    async def send_step_group(self, members, *, channel):
        ids = [m["id"] for m in members]
        self.groups.append((channel, ids))
        batch_id = f"batch-{len(self.groups)}"
        self.batch_ids.append(batch_id)
        if self.outcome:
            return {**self.outcome(members), "batch_id": batch_id}
        return {
            "batch_id": batch_id,
            "attempted": ids,
            "already": [],
            "unaddressed": [],
            "ok": True,
            "error": None,
        }


async def _run(db, service, **kwargs):
    return await engine.process_due_steps(
        db=db, service=service, worker_id="w", now=NOW, **kwargs
    )


class TestConditionsAtDueTime:
    @pytest.mark.asyncio
    async def test_nothing_due_is_a_quiet_pass(self):
        db, svc = FakeDB([]), FakeService()
        assert await _run(db, svc) == {"claimed": 0}
        assert db.claim_kwargs == {"worker_id": "w", "limit": 200, "lease_minutes": 10}

    @pytest.mark.asyncio
    async def test_unseen_unresolved_sends(self):
        db, svc = FakeDB([_step()]), FakeService()
        stats = await _run(db, svc)
        assert svc.groups == [("email", [1])]
        assert db.states()[1] == ("done", "batch:batch-1")
        assert stats["sent"] == 1 and stats["batches"] == 1

    @pytest.mark.asyncio
    async def test_seen_row_skips_and_names_the_condition(self):
        db, svc = FakeDB([_step(seen_at=NOW)]), FakeService()
        stats = await _run(db, svc)
        assert svc.groups == []
        assert db.states()[1] == ("skipped", "condition:not_seen")
        assert stats["skipped"] == 1

    @pytest.mark.asyncio
    async def test_resolved_row_is_cancelled(self):
        db, svc = FakeDB([_step(resolved_at=NOW)]), FakeService()
        await _run(db, svc)
        assert db.states()[1] == ("cancelled", "resolved")
        assert svc.probed == []

    @pytest.mark.asyncio
    async def test_live_probe_outranks_the_row_and_is_cached_per_source(self):
        # Two steps (email + ntfy) about one job the resolve hooks never
        # stamped — the probe says it is settled: both skip, one probe call.
        db = FakeDB([_step(id=1), _step(id=2, step_kind="ntfy", step_index=1)])
        svc = FakeService(resolved=True)
        await _run(db, svc)
        assert svc.probed == [("job", "job-1")]
        assert db.states() == {
            1: ("skipped", "condition:not_resolved"),
            2: ("skipped", "condition:not_resolved"),
        }

    @pytest.mark.asyncio
    async def test_archived_row_skips(self):
        db, svc = FakeDB([_step(archived_at=NOW)]), FakeService()
        await _run(db, svc)
        assert db.states()[1] == ("skipped", "archived")

    @pytest.mark.asyncio
    async def test_unknown_condition_fails_closed(self):
        db, svc = FakeDB([_step(conditions=["not_bored"])]), FakeService()
        await _run(db, svc)
        assert db.states()[1] == ("skipped", "condition:invalid:not_bored")


class TestPreferencesAndQuietHours:
    @pytest.mark.asyncio
    async def test_unconfigured_channel_skips_with_a_suppressed_delivery(self):
        db, svc = FakeDB([_step()]), FakeService(deliverable=False)
        await _run(db, svc)
        assert db.states()[1] == ("skipped", "channel_unconfigured")
        assert svc.suppressed == [("n-1", "email", "channel_unconfigured")]

    @pytest.mark.asyncio
    async def test_category_cell_switches_the_channel_off(self):
        db = FakeDB([_step()])
        svc = FakeService(
            settings={
                "communication": {"categories": {"review_queue": {"email": False}}}
            }
        )
        await _run(db, svc)
        assert db.states()[1] == ("skipped", "preference")
        assert svc.suppressed == [("n-1", "email", "preference")]

    @pytest.mark.asyncio
    async def test_channel_default_off_switches_it_off(self):
        db, svc = FakeDB([_step()]), FakeService(channels={"email": False})
        await _run(db, svc)
        assert db.states()[1] == ("skipped", "preference")

    @pytest.mark.asyncio
    async def test_officer_recipient_has_no_channel(self):
        db, svc = FakeDB([_step(recipient_kind="officer")]), FakeService()
        await _run(db, svc)
        assert db.states()[1] == ("skipped", "recipient_kind")

    def _quiet(self):
        # 22:00–08:00 Europe/Berlin; NOW is 14:00 Berlin → set a window that
        # contains it instead: 12:00–16:00.
        return {
            "communication": {
                "quiet_hours": {
                    "enabled": True,
                    "start": "12:00",
                    "end": "16:00",
                    "timezone": "Europe/Berlin",
                }
            }
        }

    @pytest.mark.asyncio
    async def test_quiet_hours_defer_to_the_window_end_without_sending(self):
        db, svc = FakeDB([_step()]), FakeService(settings=self._quiet())
        stats = await _run(db, svc)
        assert svc.groups == []
        assert db.settled == []
        ids, due_at, detail = db.deferred[0]
        assert ids == [1] and detail == "quiet_hours"
        assert due_at == datetime(2026, 8, 26, 14, 0, tzinfo=timezone.utc)
        assert stats["deferred"] == 1

    @pytest.mark.asyncio
    async def test_critical_crosses_quiet_hours(self):
        db = FakeDB(
            [
                _step(id=1),
                _step(
                    id=2,
                    notification_id="n-2",
                    category="incident",
                    severity="critical",
                    batch_key=None,
                    conditions=["not_resolved"],
                ),
            ]
        )
        svc = FakeService(settings=self._quiet())
        await _run(db, svc)
        assert [ids for ids, _, _ in db.deferred] == [[1]]
        assert svc.groups == [("email", [2])]
        assert db.states()[2][0] == "done"


class TestBatching:
    @pytest.mark.asyncio
    async def test_same_batch_key_becomes_one_message(self):
        db = FakeDB(
            [
                _step(id=1, notification_id="n-1"),
                _step(id=2, notification_id="n-2", source_id="job-2"),
                _step(id=3, notification_id="n-3", source_id="job-3"),
            ]
        )
        svc = FakeService()
        stats = await _run(db, svc)
        assert svc.groups == [("email", [1, 2, 3])]
        assert {v for v in db.states().values()} == {("done", "batch:batch-1")}
        assert stats == {"claimed": 3, "done": 3, "sent": 3, "batches": 1}

    @pytest.mark.asyncio
    async def test_recipient_channel_and_key_split_groups(self):
        db = FakeDB(
            [
                _step(id=1),
                _step(id=2, notification_id="n-2", step_kind="ntfy"),
                _step(id=3, notification_id="n-3", recipient_id="2" * 36),
                _step(id=4, notification_id="n-4", batch_key="incident"),
                _step(id=5, notification_id="n-5", batch_key=None),
                _step(id=6, notification_id="n-6", batch_key=None),
            ]
        )
        svc = FakeService()
        await _run(db, svc)
        assert sorted(ids for _, ids in svc.groups) == [[1], [2], [3], [4], [5], [6]]

    @pytest.mark.asyncio
    async def test_members_that_condition_out_leave_the_rest_batched(self):
        db = FakeDB(
            [
                _step(id=1),
                _step(id=2, notification_id="n-2", seen_at=NOW),
                _step(id=3, notification_id="n-3"),
            ]
        )
        svc = FakeService()
        await _run(db, svc)
        assert svc.groups == [("email", [1, 3])]
        assert db.states()[2] == ("skipped", "condition:not_seen")

    @pytest.mark.asyncio
    async def test_already_delivered_and_unaddressed_members(self):
        def outcome(members):
            return {
                "attempted": [1],
                "already": [2],
                "unaddressed": [3],
                "ok": True,
                "error": None,
            }

        db = FakeDB(
            [
                _step(id=1),
                _step(id=2, notification_id="n-2"),
                _step(id=3, notification_id="n-3"),
            ]
        )
        svc = FakeService(outcome=outcome)
        await _run(db, svc)
        states = db.states()
        assert states[1][0] == "done"
        assert states[2] == ("skipped", "already_delivered")
        assert states[3] == ("skipped", "no_email")
        assert svc.suppressed == [("n-3", "email", "no_email")]


class TestFailureLadder:
    def _failing(self, members):
        return {
            "attempted": [m["id"] for m in members],
            "already": [],
            "unaddressed": [],
            "ok": False,
            "error": "smtp down",
        }

    @pytest.mark.asyncio
    async def test_first_failure_retries_in_five_minutes(self):
        db, svc = FakeDB([_step(attempt=1)]), FakeService(outcome=self._failing)
        stats = await _run(db, svc)
        assert db.settled == []
        ids, due_at, detail = db.retried[0]
        assert ids == [1] and due_at == NOW + timedelta(minutes=5)
        assert detail == "retry:smtp down"
        assert stats["retried"] == 1

    @pytest.mark.asyncio
    async def test_ladder_then_failed(self):
        db, svc = FakeDB([_step(attempt=2)]), FakeService(outcome=self._failing)
        await _run(db, svc)
        assert db.retried[0][1] == NOW + timedelta(minutes=15)
        db, svc = FakeDB([_step(attempt=3)]), FakeService(outcome=self._failing)
        stats = await _run(db, svc)
        assert db.retried == []
        assert db.states()[1] == ("failed", "smtp down")
        assert stats["failed"] == 1

    def test_retry_due_at_ladder(self):
        assert engine.retry_due_at(1, NOW) == NOW + timedelta(minutes=5)
        assert engine.retry_due_at(2, NOW) == NOW + timedelta(minutes=15)
        assert engine.retry_due_at(3, NOW) == NOW + timedelta(minutes=45)
        assert engine.retry_due_at(4, NOW) is None
        assert engine.retry_due_at(0, NOW) is None

    def test_group_key(self):
        assert engine.group_key(_step()) == ("user", USER, "email", "review_queue")
        assert engine.group_key(_step(id=7, batch_key=None))[3] == "step:7"


class TestSendStepGroup:
    def _svc(self, *, claims=None, email=True, address="legate@example.org"):
        svc = NotificationService.__new__(NotificationService)
        svc._available = True
        svc._db = MagicMock()
        svc._cockpit_url = "https://cockpit"
        svc._transports = {}
        svc._claim_delivery = AsyncMock(side_effect=claims or (lambda *a, **k: "c"))
        svc._settle_delivery = AsyncMock()
        svc._get_user = AsyncMock(
            return_value={"id": USER, "email": address, "display_name": "L"}
        )
        svc._email_service = MagicMock()
        svc._email_service.send_notification_email = AsyncMock(
            return_value=(email, "<m@srw>" if email else None)
        )
        return svc

    @pytest.mark.asyncio
    async def test_single_member_sends_the_row_itself(self):
        svc = self._svc()
        out = await svc.send_step_group([_step()], channel="email")
        assert out["ok"] is True and out["attempted"] == [1]
        svc._claim_delivery.assert_awaited_once_with(
            "n-1",
            "email",
            address="legate@example.org",
            step_index=0,
            batch_id=out["batch_id"],
        )
        send = svc._email_service.send_notification_email
        assert send.call_args.kwargs["subject"] == "Job 1 completed — review required"
        assert send.call_args.kwargs["cockpit_path"] == "/inbox?n=n-1"
        svc._settle_delivery.assert_awaited_once_with(
            "c", state="sent", provider_msg_id="<m@srw>", error=None
        )

    @pytest.mark.asyncio
    async def test_three_members_one_digest_three_claims_sharing_the_batch(self):
        svc = self._svc()
        members = [
            _step(id=1, notification_id="n-1"),
            _step(id=2, notification_id="n-2", subject="Job 2 done"),
            _step(id=3, notification_id="n-3", subject="Job 3 done"),
        ]
        out = await svc.send_step_group(members, channel="email")
        assert out["attempted"] == [1, 2, 3]
        assert svc._email_service.send_notification_email.await_count == 1
        kwargs = svc._email_service.send_notification_email.call_args.kwargs
        assert kwargs["subject"] == "3 review queue items waiting for you"
        assert "[open](https://cockpit/inbox?n=n-2)" in kwargs["body_md"]
        assert "first line" in kwargs["body_md"]
        assert kwargs["cockpit_path"] == "/inbox"
        batch_ids = {c.kwargs["batch_id"] for c in svc._claim_delivery.call_args_list}
        assert batch_ids == {out["batch_id"]}
        assert svc._settle_delivery.await_count == 3

    @pytest.mark.asyncio
    async def test_lost_claims_are_reported_not_sent(self):
        svc = self._svc(claims=[None, "c2"])
        members = [_step(id=1), _step(id=2, notification_id="n-2")]
        out = await svc.send_step_group(members, channel="email")
        assert out["already"] == [1] and out["attempted"] == [2]
        assert svc._email_service.send_notification_email.await_count == 1

    @pytest.mark.asyncio
    async def test_all_claims_lost_sends_nothing(self):
        svc = self._svc(claims=[None])
        out = await svc.send_step_group([_step()], channel="email")
        assert out["attempted"] == [] and out["ok"] is True
        svc._email_service.send_notification_email.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_address_is_unaddressed(self):
        svc = self._svc(address=None)
        out = await svc.send_step_group([_step()], channel="email")
        assert out["unaddressed"] == [1]
        svc._claim_delivery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_provider_failure_settles_every_claim_failed(self):
        svc = self._svc(email=False)
        out = await svc.send_step_group(
            [_step(id=1), _step(id=2, notification_id="n-2")], channel="email"
        )
        assert out["ok"] is False and out["error"] == "send returned False"
        assert {c.kwargs["state"] for c in svc._settle_delivery.call_args_list} == {
            "failed"
        }

    def test_render_single_and_digest(self):
        subject, body, path = NotificationService.render_step_message(
            [_step()], cockpit_url="https://c"
        )
        assert subject == "Job 1 completed — review required"
        assert body == "first line\nsecond line" and path == "/inbox?n=n-1"
        subject, body, path = NotificationService.render_step_message(
            [_step(), _step(notification_id="n-2", body="x" * 300)],
            cockpit_url="https://c",
        )
        assert subject == "2 review queue items waiting for you"
        assert "…" in body and path == "/inbox"


class TestLoop:
    @pytest.mark.asyncio
    async def test_loop_runs_a_pass_and_stops_on_shutdown(self):
        import asyncio

        db, svc = FakeDB([_step()]), FakeService()
        stop = asyncio.Event()

        async def _stop_soon():
            await asyncio.sleep(0.05)
            stop.set()

        asyncio.get_running_loop().create_task(_stop_soon())
        await engine.notification_steps_loop(stop, db, svc, interval_seconds=60)
        assert svc.groups == [("email", [1])]

    def test_catalog_conditions_are_the_engine_vocabulary(self):
        # Every condition a class declares must be one the engine evaluates.
        for steps in cat.SEVERITY_CLASSES.values():
            for step in steps:
                for condition in step.conditions:
                    cat.validate_condition(condition)
