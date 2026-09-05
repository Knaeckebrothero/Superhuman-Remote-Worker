"""Webhook notification transports — Ntfy, Slack, Discord.

Each transport follows the NatsBridge graceful degradation pattern:
- Configured via environment variables
- ``is_configured`` property returns False when env vars are missing
- ``send()`` is a no-op that returns False when not configured
- No exceptions propagate to callers

Environment variables:
    NTFY_URL        — Ntfy server URL (e.g., https://ntfy.sh)
    NTFY_TOPIC      — Ntfy topic name
    NTFY_TOKEN      — Ntfy auth token (optional)
    SLACK_WEBHOOK_URL   — Slack incoming webhook URL
    DISCORD_WEBHOOK_URL — Discord webhook URL
"""

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class NotificationPayload:
    """Normalized notification data for all transports."""

    subject: str
    body_text: str
    job_id: str
    job_description: str = ""
    config_name: str = ""
    thread_id: str | None = None
    cockpit_url: str | None = None
    priority: str = "default"  # low, default, high, urgent


class NtfyTransport:
    """Ntfy push notification transport.

    Sends notifications via Ntfy's simple REST API.
    Supports optional token-based authentication.
    """

    def __init__(self) -> None:
        self._url = os.getenv("NTFY_URL", "").rstrip("/")
        self._topic = os.getenv("NTFY_TOPIC", "")
        self._token = os.getenv("NTFY_TOKEN", "")

    @property
    def name(self) -> str:
        return "ntfy"

    @property
    def is_configured(self) -> bool:
        return bool(self._url and self._topic)

    async def send(self, payload: NotificationPayload) -> bool:
        if not self.is_configured:
            return False

        headers: dict[str, str] = {
            "Title": payload.subject[:256],
            "Priority": payload.priority,
            "Tags": "robot",
        }
        if payload.cockpit_url:
            headers["Click"] = payload.cockpit_url
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self._url}/{self._topic}",
                    content=payload.body_text[:4096],
                    headers=headers,
                )
                resp.raise_for_status()
            logger.debug("Ntfy notification sent: %s", payload.subject)
            return True
        except Exception as e:
            logger.warning("Ntfy send failed: %s", e)
            return False


class SlackWebhookTransport:
    """Slack incoming webhook transport.

    Sends notifications using Slack's Block Kit format.
    """

    def __init__(self) -> None:
        self._webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")

    @property
    def name(self) -> str:
        return "slack_webhook"

    @property
    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    async def send(self, payload: NotificationPayload) -> bool:
        if not self.is_configured:
            return False

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"[SRW] {payload.subject}"[:150],
                },
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": payload.body_text[:3000]},
            },
        ]
        if payload.job_description:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"Job: {payload.job_description[:80]} • Agent: {payload.config_name}",
                        },
                    ],
                }
            )
        if payload.cockpit_url:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Open in Cockpit"},
                            "url": payload.cockpit_url,
                        },
                    ],
                }
            )

        body = {"blocks": blocks}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._webhook_url, json=body)
                resp.raise_for_status()
            logger.debug("Slack notification sent: %s", payload.subject)
            return True
        except Exception as e:
            logger.warning("Slack webhook send failed: %s", e)
            return False


class DiscordWebhookTransport:
    """Discord webhook transport.

    Sends notifications as Discord embeds.
    """

    def __init__(self) -> None:
        self._webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")

    @property
    def name(self) -> str:
        return "discord_webhook"

    @property
    def is_configured(self) -> bool:
        return bool(self._webhook_url)

    async def send(self, payload: NotificationPayload) -> bool:
        if not self.is_configured:
            return False

        embed = {
            "title": f"[SRW] {payload.subject}"[:256],
            "description": payload.body_text[:4096],
            "color": 0xCBA6F7,  # Catppuccin Mauve
        }
        if payload.job_description:
            embed["footer"] = {
                "text": f"Job: {payload.job_description[:80]} • Agent: {payload.config_name}",
            }
        if payload.cockpit_url:
            embed["url"] = payload.cockpit_url

        body = {"embeds": [embed]}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._webhook_url, json=body)
                resp.raise_for_status()
            logger.debug("Discord notification sent: %s", payload.subject)
            return True
        except Exception as e:
            logger.warning("Discord webhook send failed: %s", e)
            return False
