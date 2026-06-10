"""Unified notification dispatcher for agent-human communication.

Orchestrates delivery across all configured channels (email, Ntfy, Slack,
Discord) while respecting user channel preferences and quiet hours.

Sits between the send endpoint and individual transports::

    send_agent_message() → notification_service.dispatch()
                              ├── email_service.send_agent_message()
                              ├── ntfy_transport.send()
                              ├── slack_transport.send()
                              ├── discord_transport.send()
                              └── notification_feed.broadcast() (SSE)

Follows the NatsBridge graceful degradation pattern: fully optional,
all operations are no-ops when unconfigured.
"""

import logging
import os
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from services.webhook_transports import (
    DiscordWebhookTransport,
    NotificationPayload,
    NtfyTransport,
    SlackWebhookTransport,
)

logger = logging.getLogger(__name__)


class NotificationService:
    """Multi-channel notification dispatcher.

    Reads user channel preferences from ``users.settings.communication.channels``
    and dispatches to each enabled channel. Queues notifications during quiet hours.
    """

    def __init__(self) -> None:
        self._db: Any = None
        self._email_service: Any = None
        self._notification_feed: Any = None
        self._available = False

        self._cockpit_url = os.getenv(
            "COCKPIT_EXTERNAL_URL", "http://localhost:4200"
        ).rstrip("/")

        # Initialize transports (each checks its own env vars)
        self._transports = {
            "ntfy": NtfyTransport(),
            "slack_webhook": SlackWebhookTransport(),
            "discord_webhook": DiscordWebhookTransport(),
        }

        configured = [name for name, t in self._transports.items() if t.is_configured]
        if configured:
            logger.info("Webhook transports configured: %s", ", ".join(configured))
        else:
            logger.info("No webhook transports configured. Email-only notifications.")

    @property
    def is_available(self) -> bool:
        return self._available

    @property
    def configured_transports(self) -> list[str]:
        """Names of transports that have valid configuration."""
        return [name for name, t in self._transports.items() if t.is_configured]

    def connect(
        self,
        db: Any,
        email_service: Any,
        notification_feed: Any = None,
    ) -> None:
        """Store references to collaborating services.

        Args:
            db: PostgresDB instance
            email_service: EmailService instance for SMTP delivery
            notification_feed: NotificationFeedService for SSE broadcast (optional)
        """
        self._db = db
        self._email_service = email_service
        self._notification_feed = notification_feed
        self._available = True
        logger.info("NotificationService initialized")

    async def dispatch(
        self,
        user_id: str,
        job_id: str,
        subject: str,
        message_md: str,
        job_description: str,
        config_name: str,
        thread_id: str | None = None,
        phase_number: int | None = None,
        recipient_email: str | None = None,
        recipient_name: str | None = None,
        sudo_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch notification to all enabled channels.

        Respects user channel preferences and quiet hours.

        Returns:
            Dict with channel results, e.g.::

                {
                    "email": True,
                    "email_message_id": "<uuid@domain>",
                    "ntfy": True,
                    "queued": False,
                }
        """
        if not self._available:
            return {"error": "NotificationService not initialized"}

        results: dict[str, Any] = {}

        # Load user channel preferences
        user_channels = await self._get_user_channels(user_id)
        user_settings = await self._get_user_settings(user_id)

        # Check quiet hours
        if self._is_in_quiet_hours(user_settings):
            # Queue for digest delivery when quiet hours end
            await self._queue_notification(
                user_id=user_id,
                job_id=job_id,
                thread_id=thread_id,
                subject=subject,
                message=message_md,
                channels=user_channels,
            )
            results["queued"] = True
            logger.info(
                "Notification queued (quiet hours): job=%s, subject=%s",
                job_id[:8],
                subject,
            )

            # Still broadcast to cockpit SSE (in-app is not affected by quiet hours)
            await self._broadcast_sse(user_id, job_id, subject, thread_id)

            return results

        results["queued"] = False

        # Build cockpit deep link (into action center inbox)
        if thread_id:
            cockpit_link = f"{self._cockpit_url}/inbox?job={job_id}&thread={thread_id}"
        elif sudo_request_id:
            cockpit_link = f"{self._cockpit_url}/inbox?sudo={sudo_request_id}"
        else:
            cockpit_link = f"{self._cockpit_url}/inbox?job={job_id}&review=1"

        # Dispatch to email
        if user_channels.get("email", True) and self._email_service:
            try:
                email_sent, email_msg_id = await self._email_service.send_agent_message(
                    to=recipient_email or "",
                    to_name=recipient_name or "User",
                    subject=subject,
                    message_md=message_md,
                    job_id=job_id,
                    job_description=job_description,
                    config_name=config_name,
                    phase_number=phase_number,
                    thread_id=thread_id,
                    sudo_request_id=sudo_request_id,
                )
                results["email"] = email_sent
                if email_msg_id:
                    results["email_message_id"] = email_msg_id
            except Exception as e:
                logger.warning("Email dispatch failed: %s", e)
                results["email"] = False

        # Build webhook payload
        payload = NotificationPayload(
            subject=subject,
            body_text=message_md,
            job_id=job_id,
            job_description=job_description,
            config_name=config_name,
            thread_id=thread_id,
            cockpit_url=cockpit_link,
        )

        # Dispatch to webhook transports
        for name, transport in self._transports.items():
            if not transport.is_configured:
                continue
            if not user_channels.get(name, True):
                continue
            try:
                results[name] = await transport.send(payload)
            except Exception as e:
                logger.warning("Transport %s failed: %s", name, e)
                results[name] = False

        # Broadcast to cockpit notification feed (SSE)
        await self._broadcast_sse(user_id, job_id, subject, thread_id)

        return results

    async def notify_automation_auto_disabled(
        self,
        user_id: str,
        automation_id: str,
        automation_name: str,
        reason: str,
    ) -> dict[str, Any]:
        """Notify the owner that their automation was auto-disabled.

        Called from ``cron_dispatcher`` after the dispatcher trips a safety
        guard (max_fires_per_day or invalid cron expression). Sends an SSE
        event for real-time cockpit updates plus an email if the user has
        email notifications enabled. Does NOT respect quiet hours — system
        safety events should reach the owner promptly so they can fix and
        re-enable.

        The disable itself is already committed when this is called; this
        method is best-effort and its failures are non-fatal.
        """
        if not self._available:
            return {"error": "NotificationService not initialized"}

        results: dict[str, Any] = {}
        user_channels = await self._get_user_channels(user_id)

        display_name = automation_name or "(unnamed)"
        subject = f"Automation '{display_name}' was auto-paused"
        body_md = (
            f"Your automation **{display_name}** was paused automatically "
            f"because: {reason}\n\n"
            f"Edit the automation in the cockpit to fix the underlying "
            f"issue and re-enable it."
        )

        # SSE broadcast — real-time cockpit notification.
        if self._notification_feed:
            try:
                self._notification_feed.broadcast(
                    user_id=user_id,
                    event_type="automation_auto_disabled",
                    data={
                        "automation_id": automation_id,
                        "automation_name": automation_name,
                        "reason": reason,
                        "cockpit_url": f"{self._cockpit_url}/automations",
                    },
                )
                results["sse"] = True
            except Exception as e:
                logger.warning("SSE broadcast failed for auto-disable: %s", e)
                results["sse"] = False

        # Email — if the user has the email channel enabled and we have
        # SMTP configured. Failures are logged; the SSE event is the
        # primary user-visible signal in the cockpit.
        if user_channels.get("email", True) and self._email_service and self._db:
            try:
                user = await self._db.get_user(user_id)
            except Exception:
                user = None
            if user and user.get("email"):
                try:
                    results[
                        "email"
                    ] = await self._email_service.send_system_notification(
                        to=user["email"],
                        to_name=user.get("display_name") or "User",
                        subject=subject,
                        body_md=body_md,
                        cockpit_path="/automations",
                    )
                except Exception as e:
                    logger.warning("Auto-disable email failed: %s", e)
                    results["email"] = False

        return results

    async def notify_admins_user_registered(
        self,
        new_user_id: str,
        display_name: str | None = None,
        email: str | None = None,
    ) -> dict[str, Any]:
        """Notify all admins that a new user registered and awaits approval.

        Fan-out to every admin (app-side admission — an admin decides who gets
        in). SSE is sent unconditionally (the in-app feed isn't subject to quiet
        hours); email is sent per each admin's channel preference and skipped
        during that admin's quiet hours, since a registration is not
        safety-critical. Best-effort; failures are logged, never raised.

        Mirrors :py:meth:`notify_automation_auto_disabled` (direct SSE +
        ``send_system_notification``), not the job-shaped :py:meth:`dispatch`.
        """
        if not self._available or not self._db:
            return {"error": "NotificationService not initialized"}

        try:
            admin_ids = await self._db.list_admin_user_ids()
        except Exception as e:
            logger.warning("Could not list admins for registration notify: %s", e)
            return {"error": "admin lookup failed"}

        label = display_name or email or "A new user"
        suffix = f" ({email})" if email else ""
        subject = f"New user pending approval: {label}"
        body_md = (
            f"**{label}**{suffix} just registered and is awaiting approval.\n\n"
            "Review and approve pending users on the Users page in the cockpit."
        )

        results: dict[str, Any] = {"notified": 0}
        for admin_id in admin_ids:
            if admin_id == str(new_user_id):
                continue  # a self-registered admin isn't pending anyway

            # SSE — always (the in-app feed isn't quiet-houred).
            if self._notification_feed:
                try:
                    self._notification_feed.broadcast(
                        user_id=admin_id,
                        event_type="user_registered",
                        data={
                            "user_id": str(new_user_id),
                            "display_name": display_name,
                            "email": email,
                            "cockpit_url": f"{self._cockpit_url}/admin/users",
                        },
                    )
                    results["notified"] += 1
                except Exception as e:
                    logger.warning("SSE broadcast failed for user_registered: %s", e)

            # Email — per preference, skipped during the admin's quiet hours.
            if not self._email_service:
                continue
            try:
                channels = await self._get_user_channels(admin_id)
                settings = await self._get_user_settings(admin_id)
                if not channels.get("email", True) or self._is_in_quiet_hours(settings):
                    continue
                admin = await self._db.get_user(admin_id)
                if admin and admin.get("email"):
                    await self._email_service.send_system_notification(
                        to=admin["email"],
                        to_name=admin.get("display_name") or "Admin",
                        subject=subject,
                        body_md=body_md,
                        cockpit_path="/admin/users",
                    )
            except Exception as e:
                logger.warning("Registration email to admin %s failed: %s", admin_id, e)

        return results

    async def dispatch_digest(
        self,
        user_id: str,
        notifications: list[dict],
    ) -> dict[str, bool]:
        """Send a batched digest of queued notifications.

        Called by the quiet hours digest loop when quiet hours end.
        """
        if not notifications:
            return {}

        # Build digest body
        lines = [
            f"You have {len(notifications)} notification(s) from while you were away:\n"
        ]
        for n in notifications:
            lines.append(f"**{n['subject']}**")
            lines.append(f"{n['message'][:200]}")
            lines.append("")

        digest_body = "\n".join(lines)
        digest_subject = f"{len(notifications)} queued notification(s)"

        # Get user's email for the digest
        user = None
        if self._db:
            try:
                user = await self._db.get_user(user_id)
            except Exception:
                pass

        user_channels = await self._get_user_channels(user_id)
        cockpit_link = self._cockpit_url

        results: dict[str, bool] = {}

        # Send email digest
        if user_channels.get("email", True) and self._email_service and user:
            try:
                email_sent, _ = await self._email_service.send_agent_message(
                    to=user.get("email", ""),
                    to_name=user.get("display_name", "User"),
                    subject=digest_subject,
                    message_md=digest_body,
                    job_id=notifications[0].get("job_id", ""),
                    job_description="Notification Digest",
                    config_name="system",
                )
                results["email"] = email_sent
            except Exception as e:
                logger.warning("Digest email failed: %s", e)
                results["email"] = False

        # Send webhook digest
        payload = NotificationPayload(
            subject=digest_subject,
            body_text=digest_body,
            job_id=notifications[0].get("job_id", ""),
            cockpit_url=cockpit_link,
        )

        for name, transport in self._transports.items():
            if not transport.is_configured:
                continue
            if not user_channels.get(name, True):
                continue
            try:
                results[name] = await transport.send(payload)
            except Exception as e:
                logger.warning("Digest transport %s failed: %s", name, e)
                results[name] = False

        return results

    async def _get_user_channels(self, user_id: str) -> dict[str, bool]:
        """Load user's channel preferences from settings JSONB."""
        defaults = {"email": True, "cockpit": True}
        if not self._db:
            return defaults
        try:
            settings = await self._db.get_user_settings(user_id)
            channels = (settings or {}).get("communication", {}).get("channels", {})
            return {**defaults, **channels}
        except Exception:
            return defaults

    async def _get_user_settings(self, user_id: str) -> dict:
        """Load full user settings."""
        if not self._db:
            return {}
        try:
            return await self._db.get_user_settings(user_id) or {}
        except Exception:
            return {}

    @staticmethod
    def _is_in_quiet_hours(user_settings: dict) -> bool:
        """Check if current time falls within user's quiet hours."""
        qh = (user_settings or {}).get("communication", {}).get("quiet_hours", {})
        if not qh.get("enabled"):
            return False

        start_str = qh.get("start", "")
        end_str = qh.get("end", "")
        tz_str = qh.get("timezone", "UTC")

        if not start_str or not end_str:
            return False

        try:
            tz = ZoneInfo(tz_str)
        except (ZoneInfoNotFoundError, KeyError):
            logger.warning("Invalid timezone in quiet hours: %s", tz_str)
            return False

        try:
            start = time.fromisoformat(start_str)
            end = time.fromisoformat(end_str)
        except ValueError:
            return False

        now = datetime.now(tz).time()

        # Handle overnight ranges (e.g., 22:00 - 08:00)
        if start <= end:
            return start <= now <= end
        else:
            return now >= start or now <= end

    async def _queue_notification(
        self,
        user_id: str,
        job_id: str,
        thread_id: str | None,
        subject: str,
        message: str,
        channels: dict,
    ) -> None:
        """Queue a notification for later digest delivery."""
        if not self._db:
            return
        try:
            await self._db.queue_notification(
                user_id=user_id,
                job_id=job_id,
                thread_id=thread_id,
                subject=subject,
                message=message,
                channels=channels,
            )
        except Exception as e:
            logger.warning("Failed to queue notification: %s", e)

    async def _broadcast_sse(
        self,
        user_id: str,
        job_id: str,
        subject: str,
        thread_id: str | None,
    ) -> None:
        """Broadcast to cockpit notification feed (SSE)."""
        if not self._notification_feed:
            return
        try:
            self._notification_feed.broadcast(
                user_id=user_id,
                event_type="new_message",
                data={
                    "job_id": job_id,
                    "subject": subject,
                    "thread_id": thread_id,
                },
            )
        except Exception as e:
            logger.debug("SSE broadcast failed: %s", e)


# Module-level singleton
notification_service = NotificationService()
