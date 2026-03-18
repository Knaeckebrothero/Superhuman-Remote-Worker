"""Async email service for agent-human communication and system notifications.

Reads SMTP configuration from environment variables. Designed for use with
any SMTP relay (Proton Bridge, SendGrid, etc.) on the cluster network.

Environment variables:
    SMTP_HOST              — SMTP server hostname (required for email delivery)
    SMTP_PORT              — SMTP port (default: 587)
    SMTP_USER              — SMTP username
    SMTP_PASSWORD           — SMTP password
    SMTP_USE_TLS           — Use STARTTLS (default: true)
    SMTP_TRUST_SELF_SIGNED — Accept self-signed certs (default: false)
    SMTP_FROM              — Sender email address (default: noreply@example.com)
    COCKPIT_EXTERNAL_URL   — Cockpit URL for deep links in emails (default: http://localhost:4200)
"""

import logging
import os
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

logger = logging.getLogger(__name__)


class EmailService:
    """Async SMTP email sender."""

    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "yes")
        self.trust_self_signed = os.getenv("SMTP_TRUST_SELF_SIGNED", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        self.from_address = os.getenv("SMTP_FROM", "noreply@example.com")
        self.cockpit_url = os.getenv("COCKPIT_EXTERNAL_URL", "http://localhost:4200").rstrip("/")

    @property
    def is_configured(self) -> bool:
        """Whether SMTP is configured (host is set)."""
        return bool(self.host)

    def _get_tls_context(self) -> ssl.SSLContext | None:
        """Build TLS context, optionally trusting self-signed certs."""
        if not self.use_tls:
            return None
        ctx = ssl.create_default_context()
        if self.trust_self_signed:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _send(self, to: str, subject: str, body_text: str, body_html: str) -> bool:
        """Send an email via SMTP.

        Returns True on success, False on failure (logs the error).
        """
        if not self.is_configured:
            logger.warning("SMTP not configured — cannot send email")
            return False

        msg = MIMEMultipart("alternative")
        msg["From"] = self.from_address
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        try:
            tls_context = self._get_tls_context()
            await aiosmtplib.send(
                msg,
                hostname=self.host,
                port=self.port,
                username=self.user or None,
                password=self.password or None,
                start_tls=self.use_tls,
                tls_context=tls_context,
            )
            logger.info(f"Email sent to {to}: {subject}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {to}: {e}")
            return False

    async def send_agent_message(
        self,
        to: str,
        to_name: str,
        subject: str,
        message_md: str,
        job_id: str,
        job_description: str,
        config_name: str,
        phase_number: int | None = None,
        thread_id: str | None = None,
    ) -> bool:
        """Send an agent message email to a human.

        Wraps the agent's markdown message in a branded HTML template
        with job context and a cockpit deep link.

        Args:
            to: Recipient email address
            to_name: Recipient display name
            subject: Email subject (will be prefixed with [SRW])
            message_md: Message body in markdown (rendered as plain text)
            job_id: Job UUID for context and deep links
            job_description: Job description for context
            config_name: Agent config name
            phase_number: Current phase number (optional)
            thread_id: Thread ID for reply deep link (optional)

        Returns:
            True if sent successfully, False otherwise
        """
        full_subject = f"[SRW] {subject}"

        # Build cockpit link
        cockpit_link = f"{self.cockpit_url}/jobs/{job_id}"
        if thread_id:
            cockpit_link += f"/messages/{thread_id}"

        phase_str = f"Phase {phase_number}" if phase_number is not None else "—"

        # Plain text version
        body_text = (
            f"SRW Agent Message\n"
            f"Job: {job_description}\n"
            f"Agent: {config_name}, {phase_str}\n"
            f"{'=' * 50}\n\n"
            f"{message_md}\n\n"
            f"{'=' * 50}\n"
            f"Reply in Cockpit: {cockpit_link}\n"
        )

        # HTML version
        # Escape basic HTML entities in the message
        message_html = (
            message_md
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )

        body_html = f"""\
<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 600px; margin: 0 auto; padding: 40px 20px; color: #cdd6f4; background: #1e1e2e;">
  <div style="border: 1px solid #313244; border-radius: 12px; overflow: hidden;">
    <div style="background: #181825; padding: 16px 20px; border-bottom: 1px solid #313244;">
      <h2 style="margin: 0 0 4px 0; color: #cba6f7; font-size: 16px;">SRW Agent Message</h2>
      <p style="margin: 0; color: #a6adc8; font-size: 13px;">
        Job: {job_description[:80]}
        &nbsp;&bull;&nbsp; Agent: {config_name}
        &nbsp;&bull;&nbsp; {phase_str}
      </p>
    </div>
    <div style="padding: 20px; font-size: 14px; line-height: 1.6; color: #cdd6f4;">
      {message_html}
    </div>
    <div style="background: #181825; padding: 16px 20px; border-top: 1px solid #313244; text-align: center;">
      <a href="{cockpit_link}" style="display: inline-block; background: #cba6f7; color: #1e1e2e; padding: 10px 24px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 14px;">Reply in Cockpit</a>
      <p style="margin: 12px 0 0 0; color: #6c7086; font-size: 12px;">or reply directly to this email</p>
    </div>
  </div>
</div>"""

        return await self._send(to, full_subject, body_text, body_html)


# Module-level singleton
email_service = EmailService()
