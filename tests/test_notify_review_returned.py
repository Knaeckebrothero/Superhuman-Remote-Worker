"""``NotificationService.record_review_returned`` — the "nothing was approved,
it is yours now" producer (verification gate escalation and the
stale-verification sweeper), as a shape over ``record()``.

The row is the notification (unified notification system): a
``review_queue`` item about the job, resolved by whoever settles the job. The
wording that used to live in an email body is now the row's body; the reason
the gate gives is the only thing that tells the owner WHY nobody approved.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from orchestrator.services.notification_service import (
    NotificationService,
    RecordResult,
)


def _service():
    svc = NotificationService.__new__(NotificationService)
    svc._available = True
    svc.record = AsyncMock(return_value=RecordResult("n-1", True, {"in_app": True}))
    return svc


class TestRecordReviewReturned:
    @pytest.mark.asyncio
    async def test_records_a_review_queue_row_about_the_job(self):
        svc = _service()
        result = await svc.record_review_returned(
            user_id="u1", job_id="job-123", config_name="scholar"
        )
        assert result.notification_id == "n-1"
        svc.record.assert_awaited_once()
        kwargs = svc.record.await_args.kwargs
        assert kwargs["recipient_id"] == "u1"
        assert kwargs["category"] == "review_queue"
        assert kwargs["source_kind"] == "job" and kwargs["source_id"] == "job-123"
        assert kwargs["action_params"] == {"job_id": "job-123"}
        assert "scholar" in kwargs["body"]
        assert kwargs["payload"]["returned_to_manual"] is True

    @pytest.mark.asyncio
    async def test_reason_reaches_the_owner_verbatim_and_keys_the_row(self):
        """The gate's escalation reason (round cap / no progress / no verdict)
        is the only thing that tells the owner WHY nobody approved the job —
        and a different reason is a different notification."""
        svc = _service()
        reason = "Round limit reached (3) with 1 finding(s) still open (F1)."
        await svc.record_review_returned(
            user_id="u1", job_id="job-123", config_name="scholar", reason=reason
        )
        kwargs = svc.record.await_args.kwargs
        assert reason in kwargs["body"]
        assert kwargs["payload"]["reason"] == reason
        key_with_reason = kwargs["dedup_key"]
        await svc.record_review_returned(
            user_id="u1", job_id="job-123", config_name="scholar", reason="other"
        )
        assert svc.record.await_args.kwargs["dedup_key"] != key_with_reason
        assert key_with_reason.startswith("review_returned:job-123:")

    @pytest.mark.asyncio
    async def test_without_a_reason_keeps_the_pipeline_died_wording(self):
        """The sweeper's caller passes no reason — its cause IS the pipeline,
        and that wording must not regress into a dangling empty quote."""
        svc = _service()
        await svc.record_review_returned(
            user_id="u1", job_id="job-123", config_name="scholar"
        )
        body = svc.record.await_args.kwargs["body"]
        assert "the review pipeline died" in body
        assert ">" not in body  # no empty blockquote

    @pytest.mark.asyncio
    async def test_same_call_twice_is_the_same_row(self):
        svc = _service()
        await svc.record_review_returned(
            user_id="u1", job_id="job-123", config_name="scholar"
        )
        first = svc.record.await_args.kwargs["dedup_key"]
        await svc.record_review_returned(
            user_id="u1", job_id="job-123", config_name="scholar"
        )
        assert svc.record.await_args.kwargs["dedup_key"] == first
