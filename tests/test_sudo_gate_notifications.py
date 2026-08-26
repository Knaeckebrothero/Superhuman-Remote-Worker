"""The sudo gate as a notification producer (unified notification system,
slice 3): a pending NATS sudo request records one ``sudo_request`` row for
the owner of the job or thread that raised it, and nothing else about the
gate changes — the request itself is already durable, so the row is
best-effort and never raises into the gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from services import notification_catalog as cat
from services.notification_service import RecordResult
from services.sudo_gate import SudoGateService


def _gate(*, job=None, thread=None):
    gate = SudoGateService.__new__(SudoGateService)
    gate._db = MagicMock()
    gate._db.get_job = AsyncMock(return_value=job)
    gate._db.get_thread = AsyncMock(return_value=thread)
    return gate


EVENT = {
    "id": "r-1",
    "job_id": "j-1",
    "thread_id": None,
    "vm_name": "vm-7",
    "command": "apt-get",
    "arguments": ["install", "-y", "jq"],
    "requesting_user": "agent-host",
    "target_user": "root",
    "working_directory": "/workspace",
    "requested_at": "2026-08-27T00:00:00+00:00",
    "request_type": "sudo_command",
}


@pytest.fixture
def record(monkeypatch):
    from services import notification_service as ns

    stub = AsyncMock(return_value=RecordResult("n-1", True, {"in_app": True}))
    monkeypatch.setattr(ns.notification_service, "record", stub)
    return stub


class TestOwnerNotification:
    @pytest.mark.asyncio
    async def test_job_request_goes_to_the_job_owner(self, record):
        gate = _gate(job={"id": "j-1", "user_id": "owner-1"})
        await gate._record_owner_notification(
            "r-1", job_id="j-1", thread_id=None, event=EVENT
        )
        record.assert_awaited_once()
        kw = record.await_args.kwargs
        assert kw["recipient_id"] == "owner-1"
        assert kw["category"] == "sudo_request"
        assert kw["dedup_key"] == "sudo_request:r-1"
        assert (kw["source_kind"], kw["source_id"]) == ("sudo_request", "r-1")
        assert kw["action_params"] == {
            "request_id": "r-1",
            "job_id": "j-1",
            "thread_id": None,
        }
        assert kw["subject"] == "Sudo approval needed: apt-get"
        assert "`apt-get install -y jq`" in kw["body"]
        assert "vm-7" in kw["body"] and "5 minutes" in kw["body"]
        assert kw["payload"]["command"] == "apt-get"

    @pytest.mark.asyncio
    async def test_thread_request_goes_to_the_thread_owner(self, record):
        gate = _gate(thread={"id": "t-1", "user_id": "owner-2"})
        await gate._record_owner_notification(
            "r-2",
            job_id=None,
            thread_id="t-1",
            event={**EVENT, "id": "r-2", "job_id": None, "thread_id": "t-1"},
        )
        assert record.await_args.kwargs["recipient_id"] == "owner-2"
        gate._db.get_job.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_owner_records_nothing(self, record):
        gate = _gate(job={"id": "j-1", "user_id": None})
        await gate._record_owner_notification(
            "r-1", job_id="j-1", thread_id=None, event=EVENT
        )
        record.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_feed_failure_never_reaches_the_gate(self, record):
        record.side_effect = RuntimeError("feed down")
        gate = _gate(job={"id": "j-1", "user_id": "owner-1"})
        await gate._record_owner_notification(
            "r-1", job_id="j-1", thread_id=None, event=EVENT
        )  # no raise

    @pytest.mark.asyncio
    async def test_without_a_db_it_is_a_no_op(self, record):
        gate = SudoGateService.__new__(SudoGateService)
        gate._db = None
        await gate._record_owner_notification(
            "r-1", job_id="j-1", thread_id=None, event=EVENT
        )
        record.assert_not_awaited()


class TestCategoryContract:
    def test_sudo_request_is_critical_push_only(self):
        """A 300 s TTL makes mail pointless: the class overrides the critical
        steps to the push channels, immediately, crossing quiet hours."""
        spec = cat.category_spec("sudo_request")
        assert spec.severity == "critical"
        steps = cat.steps_for(spec, "critical")
        assert {s.channel for s in steps} == {
            "ntfy",
            "slack_webhook",
            "discord_webhook",
        }
        assert all(s.immediate for s in steps)
        assert cat.bypasses_quiet_hours(spec, "critical") is True
        assert [a.type for a in spec.actions] == ["approve", "deny", "open"]
        deny = spec.actions[1]
        assert deny.style == "danger" and deny.input_name == "reason"
