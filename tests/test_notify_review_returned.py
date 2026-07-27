"""Unit test for NotificationService.notify_review_returned_to_manual.

Mirrors the notify_automation_auto_disabled setup in
test_headless_notifications_phase4.py — mocked feed + email service, no real DB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.notification_service import NotificationService


def _connected_service(*, email_enabled=True, has_email=True):
    svc = NotificationService()
    feed = MagicMock()
    feed.broadcast = MagicMock()
    email = MagicMock()
    email.send_system_notification = AsyncMock(return_value=True)
    db = MagicMock()
    db.get_user = AsyncMock(
        return_value={"email": "owner@example.com", "display_name": "Owner"}
        if has_email
        else {"email": None}
    )
    svc.connect(db=db, email_service=email, notification_feed=feed)
    svc._get_user_channels = AsyncMock(return_value={"email": email_enabled})
    return svc, feed, email


class TestNotifyReviewReturnedToManual:
    @pytest.mark.asyncio
    async def test_broadcasts_sse_and_sends_email(self):
        svc, feed, email = _connected_service()

        result = await svc.notify_review_returned_to_manual(
            user_id="u1", job_id="job-123", config_name="scholar"
        )

        assert result["sse"] is True
        assert result["email"] is True
        # SSE carries the job id + a review deep-link.
        feed.broadcast.assert_called_once()
        sse_kwargs = feed.broadcast.call_args.kwargs
        assert sse_kwargs["event_type"] == "review_returned_to_manual"
        assert sse_kwargs["data"]["job_id"] == "job-123"
        # Email addressed to the owner, mentions the config label.
        email.send_system_notification.assert_awaited_once()
        mail_kwargs = email.send_system_notification.await_args.kwargs
        assert mail_kwargs["to"] == "owner@example.com"
        assert "scholar" in mail_kwargs["body_md"]

    @pytest.mark.asyncio
    async def test_skips_email_when_channel_disabled(self):
        svc, feed, email = _connected_service(email_enabled=False)

        result = await svc.notify_review_returned_to_manual(
            user_id="u1", job_id="job-123", config_name="scholar"
        )

        assert result.get("sse") is True
        email.send_system_notification.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reason_reaches_the_owner_verbatim(self):
        """The gate's escalation reason (round cap / no progress / no verdict)
        is the only thing that tells the owner WHY nobody approved the job."""
        svc, feed, email = _connected_service()

        reason = "Round limit reached (3) with 1 finding(s) still open (F1)."
        await svc.notify_review_returned_to_manual(
            user_id="u1", job_id="job-123", config_name="scholar", reason=reason
        )

        assert reason in email.send_system_notification.await_args.kwargs["body_md"]
        assert feed.broadcast.call_args.kwargs["data"]["reason"] == reason

    @pytest.mark.asyncio
    async def test_without_a_reason_keeps_the_pipeline_died_wording(self):
        """The sweeper's caller passes no reason — its cause IS the pipeline,
        and that wording must not regress into a dangling empty quote."""
        svc, feed, email = _connected_service()

        await svc.notify_review_returned_to_manual(
            user_id="u1", job_id="job-123", config_name="scholar"
        )

        body = email.send_system_notification.await_args.kwargs["body_md"]
        assert "the review pipeline died" in body
        assert ">" not in body  # no empty blockquote

    @pytest.mark.asyncio
    async def test_returns_error_when_not_connected(self):
        svc = NotificationService()  # not connected → _available False

        result = await svc.notify_review_returned_to_manual(
            user_id="u1", job_id="job-123", config_name="scholar"
        )

        assert "error" in result
