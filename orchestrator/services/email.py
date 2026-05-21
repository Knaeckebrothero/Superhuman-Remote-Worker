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
    AGENT_EMAIL            — Agent email for IMAP reply routing (e.g., agent@example.com)
    MAIL_DOMAIN            — Mail domain for Message-ID generation (e.g., example.com)
"""

import logging
import os
import ssl
import uuid as uuid_mod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

logger = logging.getLogger(__name__)


# RFC 6761 §6 reserved TLDs that are guaranteed not to resolve in the public
# DNS. SMTP bridges typically accept envelopes addressed to them at the
# protocol level (RCPT TO parses, server says 250) without actually being
# able to deliver, so without this guard we record delivery_status='sent'
# for messages that physically cannot reach an inbox.
_RFC6761_RESERVED_TLDS = frozenset({"invalid", "test", "example", "localhost"})


def _is_undeliverable_recipient(addr: str) -> bool:
    """True if `addr` is structurally undeliverable (malformed, or in a
    reserved TLD per RFC 6761). Cheap syntactic check — no DNS lookup."""
    if not addr or "@" not in addr:
        return True
    local, _, domain = addr.rpartition("@")
    if not local or not domain or domain.startswith(".") or ".." in domain:
        return True
    tld = domain.rsplit(".", 1)[-1].lower()
    return tld in _RFC6761_RESERVED_TLDS


class EmailService:
    """Async SMTP email sender."""

    def __init__(self) -> None:
        self.host = os.getenv("SMTP_HOST", "")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER", "")
        self.password = os.getenv("SMTP_PASSWORD", "")
        self.use_tls = os.getenv("SMTP_USE_TLS", "true").lower() in ("true", "1", "yes")
        self.trust_self_signed = os.getenv(
            "SMTP_TRUST_SELF_SIGNED", "false"
        ).lower() in (
            "true",
            "1",
            "yes",
        )
        self.from_address = os.getenv("SMTP_FROM", "noreply@example.com")
        self.cockpit_url = os.getenv(
            "COCKPIT_EXTERNAL_URL", "http://localhost:4200"
        ).rstrip("/")
        self.agent_email = os.getenv("AGENT_EMAIL", "")
        self.mail_domain = os.getenv("MAIL_DOMAIN", "")

    @property
    def is_configured(self) -> bool:
        """Whether SMTP is configured (host is set)."""
        return bool(self.host)

    @property
    def reply_routing_configured(self) -> bool:
        """Whether IMAP reply routing is available (agent email + mail domain set)."""
        return bool(self.agent_email and self.mail_domain)

    def _get_tls_context(self) -> ssl.SSLContext | None:
        """Build TLS context, optionally trusting self-signed certs."""
        if not self.use_tls:
            return None
        ctx = ssl.create_default_context()
        if self.trust_self_signed:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def _send(
        self,
        to: str | list[str],
        subject: str,
        body_text: str,
        body_html: str,
        reply_to: str | None = None,
        message_id: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
    ) -> bool:
        """Send an email via SMTP.

        Args:
            to: Recipient email address(es)
            subject: Email subject
            body_text: Plain text body
            body_html: HTML body
            reply_to: Reply-To header (for IMAP routing)
            message_id: RFC822 Message-ID header
            in_reply_to: In-Reply-To header for threading
            references: References header for threading

        Returns True on success, False on failure (logs the error).
        """
        if not self.is_configured:
            logger.warning("SMTP not configured — cannot send email")
            return False

        recipients = to if isinstance(to, list) else [to]
        bad = [r for r in recipients if _is_undeliverable_recipient(r)]
        if bad:
            logger.warning(
                "Refusing to send email to undeliverable recipient(s): %s", bad
            )
            return False

        msg = MIMEMultipart("alternative")
        msg["From"] = self.from_address
        msg["To"] = ", ".join(to) if isinstance(to, list) else to
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        if message_id:
            msg["Message-ID"] = message_id
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
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
            logger.info(f"Email sent to {recipients}: {subject}")
            return True
        except Exception as e:
            logger.error(f"Failed to send email to {recipients}: {e}")
            return False

    async def send_system_notification(
        self,
        to: str,
        to_name: str,
        subject: str,
        body_md: str,
        *,
        cockpit_path: str = "",
    ) -> bool:
        """Send a non-job system notification email.

        For events that aren't tied to a specific job — automation auto-disable,
        account-level alerts — where ``send_agent_message``'s job-shaped
        headers (job_id, config_name, reply-routing) don't apply.

        Args:
            to: Recipient email address
            to_name: Recipient display name
            subject: Subject line (prefixed with [SRW])
            body_md: Body in markdown (rendered as plain text in HTML)
            cockpit_path: Optional path appended to COCKPIT_EXTERNAL_URL
                for the "Open Cockpit" link (e.g. "/automations").

        Returns True on success, False on failure.
        """
        full_subject = f"[SRW] {subject}"
        cockpit_link = (
            f"{self.cockpit_url}{cockpit_path}" if cockpit_path else self.cockpit_url
        )

        body_text = (
            f"Hello {to_name},\n\n"
            f"{body_md}\n\n"
            f"{'=' * 50}\n"
            f"Open Cockpit: {cockpit_link}\n"
        )

        body_html_msg = (
            body_md.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        body_html = (
            '<!DOCTYPE html><html><body style="font-family: sans-serif; color: #222;">'
            f"<p>Hello {to_name},</p>"
            f"<p>{body_html_msg}</p>"
            "<hr>"
            f'<p style="color: #666;"><a href="{cockpit_link}">Open Cockpit</a></p>'
            "</body></html>"
        )

        return await self._send(
            to=to,
            subject=full_subject,
            body_text=body_text,
            body_html=body_html,
        )

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
        sudo_request_id: str | None = None,
    ) -> tuple[bool, str | None]:
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
            Tuple of (success: bool, email_message_id: str | None).
            The message_id is the RFC822 Message-ID set on the outbound email,
            returned so the caller can store it for IMAP reply correlation.
        """
        full_subject = f"[SRW] {subject}"

        # Generate RFC822 Message-ID for reply correlation
        domain = self.mail_domain or "srw.local"
        email_msg_id = f"<{uuid_mod.uuid4().hex}@{domain}>"

        # Build Reply-To with + sub-addressing for IMAP routing
        reply_to_addr = None
        if self.reply_routing_configured and thread_id:
            agent_local = self.agent_email.split("@")[0]
            job_short = job_id[:8]
            reply_to_addr = f"{agent_local}+{job_short}+{thread_id}@{self.mail_domain}"

        # Build cockpit link (deep-link into action center inbox)
        if thread_id:
            cockpit_link = f"{self.cockpit_url}/inbox?job={job_id}&thread={thread_id}"
        elif sudo_request_id:
            cockpit_link = f"{self.cockpit_url}/inbox?sudo={sudo_request_id}"
        else:
            cockpit_link = f"{self.cockpit_url}/inbox?job={job_id}&review=1"

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
        if reply_to_addr:
            body_text += "Or reply directly to this email.\n"

        # HTML version
        # Escape basic HTML entities in the message
        message_html = (
            message_md.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )

        reply_hint = (
            '<p style="margin: 12px 0 0 0; color: #6c7086; font-size: 12px;">or reply directly to this email</p>'
            if reply_to_addr
            else ""
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
      {reply_hint}
    </div>
  </div>
</div>"""

        success = await self._send(
            to,
            full_subject,
            body_text,
            body_html,
            reply_to=reply_to_addr,
            message_id=email_msg_id,
        )
        return success, email_msg_id if success else None


# Module-level singleton
email_service = EmailService()
